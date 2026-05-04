#!/usr/bin/env python3
"""Generate a daily journalism monitoring report and GitHub Pages index."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import textwrap
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "reports"
DOCS_DIR = PROJECT_ROOT / "docs"
DOCS_REPORTS_DIR = DOCS_DIR / "reports"

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
QUERY = (
    '("social media ban" OR "social media restriction" OR "phone ban" OR '
    '"mobile phone ban" OR "age verification" OR "under-16" OR "minors") '
    '(school OR schools OR government OR regulator OR parliament OR children OR minors)'
)

OFFICIAL_OR_RECOGNIZED_DOMAINS = {
    "apnews.com": "Associated Press",
    "reuters.com": "Reuters",
    "bbc.com": "BBC",
    "bbc.co.uk": "BBC",
    "theguardian.com": "The Guardian",
    "gov.uk": "GOV.UK",
    "ofcom.org.uk": "Ofcom",
    "esafety.gov.au": "eSafety Commissioner",
    "digital-strategy.ec.europa.eu": "Comision Europea",
    "ec.europa.eu": "Comision Europea",
    "governor.ny.gov": "Gobernacion de Nueva York",
}


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    domain: str
    source_country: str
    language: str
    seen_date: str


def fetch_gdelt_articles(max_records: int = 50, timespan: str = "3d") -> list[Article]:
    params = {
        "query": QUERY,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max_records),
        "timespan": timespan,
        "sort": "hybridrel",
    }
    url = GDELT_ENDPOINT + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "journalism-monitoring-pipeline/1.0"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))

    articles = []
    for item in payload.get("articles", []):
        articles.append(
            Article(
                title=str(item.get("title", "")).strip(),
                url=str(item.get("url", "")).strip(),
                domain=str(item.get("domain", "")).strip().lower(),
                source_country=str(item.get("sourcecountry", "")).strip(),
                language=str(item.get("language", "")).strip(),
                seen_date=str(item.get("seendate", "")).strip(),
            )
        )
    return [article for article in articles if article.title and article.url]


def score_article(article: Article) -> int:
    text = f"{article.title} {article.domain}".lower()
    score = 0
    if article.domain in OFFICIAL_OR_RECOGNIZED_DOMAINS:
        score += 40
    for term in ("ban", "restriction", "restrict", "age verification", "under-16", "minor", "children"):
        if term in text:
            score += 8
    for term in ("school", "government", "regulator", "parliament", "court", "law"):
        if term in text:
            score += 6
    return score


def select_articles(articles: list[Article], limit: int = 12) -> list[Article]:
    seen_urls: set[str] = set()
    deduped: list[Article] = []
    for article in sorted(articles, key=score_article, reverse=True):
        if article.url in seen_urls:
            continue
        seen_urls.add(article.url)
        deduped.append(article)
        if len(deduped) >= limit:
            break
    return deduped


def infer_case_fields(article: Article) -> dict[str, str]:
    text = f"{article.title} {article.domain}".lower()
    country = article.source_country or "No identificado automaticamente"
    institution = "No identificado automaticamente"
    platform = "Redes sociales / dispositivos digitales"
    restriction_type = "Restriccion o debate regulatorio"

    if "australia" in text or article.domain.endswith(".gov.au"):
        country = "Australia"
        institution = "eSafety Commissioner / Gobierno australiano"
        restriction_type = "Restriccion de edad para cuentas de redes sociales"
    elif "uk" in text or "britain" in text or "england" in text or article.domain in {"gov.uk", "ofcom.org.uk"}:
        country = "Reino Unido"
        institution = "Gobierno / regulador"
    elif "new mexico" in text:
        country = "Estados Unidos / Nuevo Mexico"
        institution = "Fiscalia / tribunal"
        platform = "Meta"
        restriction_type = "Remedios judiciales o medidas de seguridad infantil"
    elif "european" in text or "eu " in text or "meta" in text and "dsa" in text:
        country = "Union Europea"
        institution = "Comision Europea"
        platform = "Meta / Instagram / Facebook"
        restriction_type = "Cumplimiento regulatorio y verificacion de edad"

    if "phone" in text or "smartphone" in text or "mobile" in text:
        platform = "Telefonos moviles o dispositivos conectados"
        restriction_type = "Restriccion de uso en escuelas"
    if "telegram" in text:
        platform = "Telegram"
    if "tiktok" in text:
        platform = "TikTok"
    if "instagram" in text or "facebook" in text or "meta" in text:
        platform = "Meta / Instagram / Facebook"

    return {
        "country": country,
        "institution": institution,
        "platform": platform,
        "restriction_type": restriction_type,
    }


def article_summary(article: Article) -> str:
    title = article.title.rstrip(".")
    return (
        f"El articulo reporta un caso relacionado con limites, restricciones o supervision "
        f"sobre redes sociales o dispositivos digitales: {title}."
    )


def normalize_date(value: str) -> str:
    match = re.match(r"(\d{8})", value)
    if not match:
        return value or "Fecha no disponible"
    raw = match.group(1)
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def is_under_16_social_media_case(article: Article) -> bool:
    text = f"{article.title} {article.domain}".lower()
    age_terms = (
        "under-16",
        "under 16",
        "under sixteen",
        "menores de 16",
        "menor de 16",
        "under-15",
        "under 15",
        "under-14",
        "under 14",
    )
    social_terms = ("social media", "social network", "instagram", "facebook", "tiktok", "snapchat", "meta")
    return any(term in text for term in age_terms) and any(term in text for term in social_terms)


def build_under_16_country_counts(articles: list[Article]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for article in articles:
        if not is_under_16_social_media_case(article):
            continue
        country = infer_case_fields(article)["country"]
        if country == "No identificado automaticamente":
            continue
        counts[country] = counts.get(country, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def build_report(articles: list[Article], run_date: str) -> str:
    under_16_counts = build_under_16_country_counts(articles)
    lines = [
        "# Monitoreo periodistico: restricciones a redes sociales y dispositivos digitales",
        "",
        f"Fecha de ejecucion: {run_date}",
        "Fuente automatizada inicial: GDELT DOC 2.0 API.",
        "Nota metodologica: las publicaciones en redes sociales no se usan como evidencia primaria.",
        "",
        "## Alcance y verificacion",
        "",
        "- Datos verificados automaticamente: titulo, enlace, dominio, fecha detectada por GDELT y pais de fuente cuando esta disponible.",
        "- Interpretaciones: pais/institucion/plataforma/tipo de restriccion se infieren desde titulo, dominio y metadatos; requieren revision editorial antes de publicacion externa.",
        "- Incertidumbres: el script no puede confirmar por si solo el texto completo de cada articulo ni reemplaza la verificacion manual con fuentes primarias.",
        "",
        "## Resumen visual: menores de 16 anos",
        "",
        f"Total de paises detectados con senales de prohibicion o restriccion de redes sociales para menores de 16 anos: {len(under_16_counts)}.",
        "",
        "| Pais / region | Casos detectados | Grafico |",
        "|---|---:|---|",
    ]

    if not under_16_counts:
        lines.append("| Sin resultados suficientemente claros | 0 | - |")
    for country, count in under_16_counts.items():
        lines.append(f"| {country} | {count} | {'#' * count} |")

    lines.extend(
        [
            "",
            "Nota: este grafico cuenta paises detectados en los articulos seleccionados por el monitoreo automatizado. Requiere verificacion editorial antes de afirmarlo como estado legal vigente.",
            "",
        "## Tabla base",
        "",
        "| Pais / region | Institucion | Plataforma | Tipo de restriccion | Fecha | Fuente | Enlace |",
        "|---|---|---|---|---|---|---|",
        ]
    )

    if not articles:
        lines.append("| Sin resultados suficientes | - | - | - | - | - | - |")
    for article in articles:
        fields = infer_case_fields(article)
        source = OFFICIAL_OR_RECOGNIZED_DOMAINS.get(article.domain, article.domain or "Fuente no identificada")
        lines.append(
            "| {country} | {institution} | {platform} | {restriction_type} | {date} | {source} | [Abrir]({url}) |".format(
                country=fields["country"],
                institution=fields["institution"],
                platform=fields["platform"],
                restriction_type=fields["restriction_type"],
                date=normalize_date(article.seen_date),
                source=source,
                url=article.url,
            )
        )

    lines.extend(["", "## Casos seleccionados", ""])
    if not articles:
        lines.append("No se encontraron resultados suficientemente relevantes en la consulta automatizada.")
    for index, article in enumerate(articles, start=1):
        fields = infer_case_fields(article)
        source = OFFICIAL_OR_RECOGNIZED_DOMAINS.get(article.domain, article.domain or "Fuente no identificada")
        lines.extend(
            [
                f"### {index}. {article.title}",
                "",
                f"- Pais / region: {fields['country']}",
                f"- Institucion: {fields['institution']}",
                f"- Plataforma: {fields['platform']}",
                f"- Tipo de restriccion: {fields['restriction_type']}",
                f"- Fecha detectada: {normalize_date(article.seen_date)}",
                f"- Fuente: {source}",
                f"- Enlace: {article.url}",
                "",
                f"**Resumen en espanol:** {article_summary(article)}",
                "",
                "**Datos verificados:** titulo, URL, dominio y fecha detectada por GDELT.",
                "",
                "**Interpretacion:** clasificacion editorial preliminar generada automaticamente.",
                "",
                "**Incertidumbres:** revisar fuente primaria o texto completo antes de usar como evidencia final.",
                "",
            ]
        )

    lines.extend(
        [
            "## Fuentes y trazabilidad",
            "",
            "- GDELT DOC 2.0 API: https://api.gdeltproject.org/api/v2/doc/doc",
            "- Documentacion GDELT DOC 2.0: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/",
            "- Fecha de extraccion registrada en este informe.",
        ]
    )
    return "\n".join(lines) + "\n"


def markdown_to_html(markdown_text: str, title: str) -> str:
    body_parts: list[str] = []
    in_table = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        if not line:
            if in_table:
                body_parts.append("</tbody></table>")
                in_table = False
            continue
        if line.startswith("|") and line.endswith("|"):
            cells = [html.escape(cell.strip()) for cell in line.strip("|").split("|")]
            if set(cells[0]) == {"-"}:
                continue
            if not in_table:
                body_parts.append("<table><tbody>")
                in_table = True
            tag = "th" if not body_parts[-1].startswith("<tr>") and "Pais / region" in cells[0] else "td"
            body_parts.append("<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>")
            continue
        if in_table:
            body_parts.append("</tbody></table>")
            in_table = False
        if line.startswith("# "):
            body_parts.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body_parts.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            body_parts.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("- "):
            body_parts.append(f"<p class=\"bullet\">{html.escape(line)}</p>")
        else:
            body_parts.append(f"<p>{html.escape(line)}</p>")
    if in_table:
        body_parts.append("</tbody></table>")

    return HTML_TEMPLATE.format(title=html.escape(title), body="\n".join(body_parts))


HTML_TEMPLATE = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="../styles.css">
</head>
<body>
  <main class="report">
    <p><a href="../index.html">Volver al indice</a></p>
    {body}
  </main>
</body>
</html>
"""


def build_index() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_links = []
    for report in sorted(DOCS_REPORTS_DIR.glob("*.html"), reverse=True):
        report_links.append(
            f'<li><a href="reports/{html.escape(report.name)}">{html.escape(report.stem)}</a></li>'
        )
    if not report_links:
        report_links.append("<li>No hay informes publicados todavia.</li>")

    index_html = f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Monitoreo periodistico de redes sociales</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <main class="home">
    <h1>Monitoreo periodistico de redes sociales</h1>
    <p>Informes diarios sobre restricciones, limites y acciones regulatorias relacionadas con redes sociales, escuelas, instituciones publicas y menores.</p>
    <section>
      <h2>Informes disponibles</h2>
      <ul>
        {chr(10).join(report_links)}
      </ul>
    </section>
    <section>
      <h2>Nota metodologica</h2>
      <p>Los resultados automatizados requieren revision editorial. Las publicaciones de redes sociales solo deben usarse como material suplementario y nunca como evidencia primaria sin verificacion independiente.</p>
    </section>
  </main>
</body>
</html>
"""
    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")


def ensure_styles() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    css_path = DOCS_DIR / "styles.css"
    if css_path.exists():
        return
    css_path.write_text(
        textwrap.dedent(
            """
            :root {
              color-scheme: light;
              --bg: #f6f7f9;
              --text: #17202a;
              --muted: #5f6b7a;
              --line: #d9dee7;
              --accent: #1f6feb;
              --panel: #ffffff;
            }
            * { box-sizing: border-box; }
            body {
              margin: 0;
              font-family: Arial, Helvetica, sans-serif;
              color: var(--text);
              background: var(--bg);
              line-height: 1.55;
            }
            main {
              width: min(1080px, calc(100% - 32px));
              margin: 32px auto;
              background: var(--panel);
              border: 1px solid var(--line);
              border-radius: 8px;
              padding: 28px;
            }
            h1, h2, h3 { line-height: 1.2; }
            h1 { margin-top: 0; font-size: 2rem; }
            h2 { margin-top: 2rem; border-top: 1px solid var(--line); padding-top: 1.25rem; }
            a { color: var(--accent); }
            table {
              width: 100%;
              border-collapse: collapse;
              margin: 1rem 0;
              font-size: 0.92rem;
            }
            th, td {
              border: 1px solid var(--line);
              padding: 8px;
              vertical-align: top;
            }
            th { background: #eef3fb; text-align: left; }
            .bullet { margin-left: 1rem; }
            @media (max-width: 760px) {
              main { width: 100%; margin: 0; border: 0; border-radius: 0; padding: 18px; }
              table { display: block; overflow-x: auto; white-space: nowrap; }
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def publish_report(markdown_text: str, report_name: str) -> None:
    DOCS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    html_text = markdown_to_html(markdown_text, report_name)
    (DOCS_REPORTS_DIR / report_name.replace(".md", ".html")).write_text(html_text, encoding="utf-8")


def import_existing_reports() -> None:
    ensure_styles()
    DOCS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    for markdown_path in sorted(REPORTS_DIR.glob("monitoreo-redes-sociales-*.md")):
        markdown_text = markdown_path.read_text(encoding="utf-8")
        publish_report(markdown_text, markdown_path.name)
    build_index()


def run_pipeline() -> int:
    run_date = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_styles()
    articles = select_articles(fetch_gdelt_articles())
    report_text = build_report(articles, run_date)
    report_name = f"monitoreo-redes-sociales-{run_date}.md"
    report_path = REPORTS_DIR / report_name
    if report_path.exists():
        stamp = datetime.now().strftime("%H%M")
        report_name = f"monitoreo-redes-sociales-{run_date}-{stamp}.md"
        report_path = REPORTS_DIR / report_name
    report_path.write_text(report_text, encoding="utf-8")
    publish_report(report_text, report_name)
    import_existing_reports()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate social media restrictions monitoring reports.")
    parser.add_argument("--import-existing", action="store_true", help="Publish existing Markdown reports to docs.")
    args = parser.parse_args()
    if args.import_existing:
        import_existing_reports()
        return 0
    return run_pipeline()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
