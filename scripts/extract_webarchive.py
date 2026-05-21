#!/usr/bin/env python3
"""Extract a Safari .webarchive into a static site."""

from __future__ import annotations

import argparse
import hashlib
import html
import mimetypes
import plistlib
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from posixpath import basename
from urllib.parse import unquote, urlparse


PREVIEW_HOST_FRAGMENT = "sitebuilderhostsite.net"


@dataclass(frozen=True)
class Resource:
    url: str
    mime: str
    data: bytes
    encoding: str
    local_path: Path


def choose_page(archive: dict) -> tuple[dict, list[dict]]:
    """Use the largest HTML subframe when available, otherwise the main page."""
    candidates: list[tuple[int, dict, list[dict]]] = []

    main = archive.get("WebMainResource")
    if main and main.get("WebResourceMIMEType") == "text/html":
        candidates.append((len(main.get("WebResourceData", b"")), main, archive.get("WebSubresources", [])))

    for subframe in archive.get("WebSubframeArchives", []):
        sub_main = subframe.get("WebMainResource", {})
        if sub_main.get("WebResourceMIMEType") == "text/html":
            candidates.append(
                (
                    len(sub_main.get("WebResourceData", b"")),
                    sub_main,
                    subframe.get("WebSubresources", []),
                )
            )

    if not candidates:
        raise RuntimeError("No HTML resource found in webarchive.")

    return max(candidates, key=lambda item: item[0])[1:]


def extension_for(url: str, mime: str) -> str:
    suffix = Path(urlparse(url).path).suffix
    if suffix:
        return suffix

    guessed = mimetypes.guess_extension(mime.split(";")[0].strip())
    if guessed:
        return guessed

    fallback = {
        "text/css": ".css",
        "text/html": ".html",
        "text/javascript": ".js",
        "application/javascript": ".js",
        "font/woff2": ".woff2",
        "image/webp": ".webp",
        "image/png": ".png",
        "image/jpeg": ".jpg",
    }
    return fallback.get(mime, ".bin")


def folder_for(mime: str) -> Path:
    if "css" in mime:
        return Path("assets/css")
    if "javascript" in mime:
        return Path("assets/js")
    if mime.startswith("font/"):
        return Path("assets/fonts")
    if mime.startswith("image/"):
        return Path("assets/images")
    return Path("assets/other")


def safe_stem(url: str, mime: str) -> str:
    parsed = urlparse(url)
    path_name = basename(parsed.path.rstrip("/"))

    if "fonts.sitebuilderhost.net/css" in url:
        return "fonts"
    if "webfontloader" in url:
        return "webfontloader"

    stem = Path(path_name).stem if path_name else ""
    if not stem or stem in {"css", "js"}:
        stem = parsed.netloc.replace(".", "-") or "resource"

    stem = unquote(stem).lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-._")
    return stem or "resource"


def local_path_for(url: str, mime: str, used: set[Path]) -> Path:
    folder = folder_for(mime)
    ext = extension_for(url, mime)
    stem = safe_stem(url, mime)
    candidate = folder / f"{stem}{ext}"

    if candidate not in used:
        used.add(candidate)
        return candidate

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    candidate = folder / f"{stem}-{digest}{ext}"
    used.add(candidate)
    return candidate


def collect_resources(raw_resources: list[dict]) -> list[Resource]:
    used: set[Path] = set()
    resources: list[Resource] = []

    for item in raw_resources:
        data = item.get("WebResourceData")
        url = item.get("WebResourceURL", "")
        mime = item.get("WebResourceMIMEType", "application/octet-stream")
        if not data or not url:
            continue

        resources.append(
            Resource(
                url=url,
                mime=mime,
                data=data,
                encoding=item.get("WebResourceTextEncodingName") or "utf-8",
                local_path=local_path_for(url, mime, used),
            )
        )

    return resources


def aliases_for(resource: Resource) -> set[str]:
    parsed = urlparse(resource.url)
    aliases = {
        resource.url,
        html.escape(resource.url, quote=True),
    }

    # Avoid broad aliases such as /css for query-driven endpoints; they can
    # corrupt dynamic strings like https://host/css?family=...
    path_is_specific = parsed.path not in {"", "/", "/css", "/js"}

    if parsed.path and path_is_specific:
        aliases.add(parsed.path)
        aliases.add(html.escape(parsed.path, quote=True))
        if parsed.query:
            path_query = f"{parsed.path}?{parsed.query}"
            aliases.add(path_query)
            aliases.add(html.escape(path_query, quote=True))

    if resource.mime.startswith("image/"):
        name = basename(parsed.path)
        if name:
            aliases.add(f"/ws/alt-imgs/w2000/{name}")
            aliases.add(f"/ws/alt-imgs/orig/{name}")

    return {alias for alias in aliases if alias}


def replace_image_family_urls(text: str, from_file: Path, resources: list[Resource]) -> str:
    for resource in resources:
        if not resource.mime.startswith("image/"):
            continue

        image_ids = re.findall(r"[0-9a-f]{32}", resource.url, flags=re.IGNORECASE)
        for image_id in image_ids:
            target = relative_target(from_file, resource.local_path)
            patterns = [
                rf"/ws/media-library/{re.escape(image_id)}/[^\"'\s,)]+",
                rf"/ws/alt-imgs/(?:orig|w[0-9]+)/[^\"'\s,)]*{re.escape(image_id)}[^\"'\s,)]*",
            ]
            for pattern in patterns:
                text = re.sub(pattern, target, text)

    return text


def build_alias_map(resources: list[Resource]) -> dict[str, Path]:
    alias_map: dict[str, Path] = {}
    for resource in resources:
        for alias in aliases_for(resource):
            alias_map[alias] = resource.local_path
    return alias_map


def relative_target(from_file: Path, target: Path) -> str:
    start = from_file.parent if from_file.name else from_file
    return Path(shutil.os.path.relpath(target, start)).as_posix()


def replace_resource_urls(
    text: str,
    from_file: Path,
    alias_map: dict[str, Path],
    resources: list[Resource],
) -> str:
    for alias, target in sorted(alias_map.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(alias, relative_target(from_file, target))

    text = replace_image_family_urls(text, from_file, resources)
    text = text.replace('href="/"', 'href="./"')
    text = text.replace("href='/'", "href='./'")
    text = text.replace('href="/#', 'href="#')
    text = text.replace("href='/#", "href='#")
    return text


def prune_external_font_faces(css: str) -> str:
    block_pattern = re.compile(r"(?:/\*.*?\*/\s*)?@font-face\s*\{.*?\}\s*", re.DOTALL)

    def keep_local(match: re.Match[str]) -> str:
        block = match.group(0)
        return "" if re.search(r"url\(\s*https?://", block, re.IGNORECASE) else block

    return block_pattern.sub(keep_local, css)


def cleanup_html(html_text: str) -> str:
    html_text = re.sub(
        r"<meta\s+content=[\"']noindex[\"']\s+name=[\"']robots[\"']>\s*",
        "",
        html_text,
        flags=re.IGNORECASE,
    )
    html_text = re.sub(
        r"urls:\s*\[\s*'https://fonts\.sitebuilderhost\.net/css\?family='\s*\+\s*usedFontsUrl\s*\+\s*'&display=swap'\s*\]",
        "urls:['assets/css/fonts.css']",
        html_text,
    )
    return html_text


def existing_image(output_dir: Path, name: str) -> Path | None:
    path = output_dir / "assets/images" / name
    return Path("assets/images") / name if path.exists() else None


def existing_image_by_fragment(output_dir: Path, fragment: str) -> Path | None:
    image_dir = output_dir / "assets/images"
    if not image_dir.exists():
        return None

    for path in sorted(image_dir.iterdir()):
        normalized = unicodedata.normalize("NFC", path.name)
        if fragment in normalized:
            return Path("assets/images") / path.name

    return None


def html_path(path: Path) -> str:
    return html.escape(path.as_posix(), quote=True)


def image_tag(path: Path, alt: str = "") -> str:
    return (
        f'<img alt="{html.escape(alt, quote=True)}" '
        f'class="ws-visible ws-export-media-image" '
        f'src="{html_path(path)}">'
    )


def replace_known_local_images(html_text: str, output_dir: Path) -> str:
    image_7399 = existing_image(output_dir, "IMG_7399.jpeg")
    image_7389 = existing_image(output_dir, "IMG_7389.jpeg")

    replacements: list[tuple[list[str], Path | None]] = [
        (
            [
                "/ws/alt-imgs/orig/f31ac04ec043dece675a8e8111cd079e.webp",
                "/ws/media-library/f31ac04ec043dece675a8e8111cd079e/img_7399.jpeg",
            ],
            image_7399,
        ),
        (
            [
                "/ws/alt-imgs/w2000/24165e707a24ff6c4c7f5b06b2035c69.webp",
                "/ws/alt-imgs/orig/24165e707a24ff6c4c7f5b06b2035c69.webp",
                "/ws/media-library/template-switch/ws-intense-next-interior-design-dark/11f116d8-e078-752a-b4e0-bc24118bdf96/blocks/custom/images/image-1.jpg",
            ],
            image_7399,
        ),
        (
            [
                "/ws/alt-imgs/w2000/0238f7766fd6d0d13d2ac1334de2de76.ws-intense-next-guesthouse.webp",
                "/ws/alt-imgs/orig/0238f7766fd6d0d13d2ac1334de2de76.ws-intense-next-guesthouse.webp",
                "/ws/blocks/custom/images/image-1.ws-intense-next-guesthouse.jpg",
            ],
            image_7389,
        ),
    ]

    for urls, path in replacements:
        if path is None:
            continue
        replacement = html_path(path)
        for url in urls:
            html_text = html_text.replace(url, replacement)

    return html_text


def image_for_media_slots(output_dir: Path) -> list[Path]:
    candidates = [
        existing_image(output_dir, "IMG_7391.jpeg"),
        existing_image(output_dir, "IMG_7399.jpeg"),
        existing_image_by_fragment(output_dir, "2026-05-15 kl. 21.05.14"),
        existing_image_by_fragment(output_dir, "2026-05-15 kl. 21.05.08"),
        existing_image_by_fragment(output_dir, "2026-05-03 kl. 11.25.15"),
        existing_image(output_dir, "IMG_7216.jpeg"),
        existing_image_by_fragment(output_dir, "2026-04-13 kl. 14.55.01"),
        existing_image_by_fragment(output_dir, "2026-05-02 kl. 21.46.36"),
        existing_image_by_fragment(output_dir, "2026-04-13 kl. 14.54.54"),
        existing_image(output_dir, "IMG_7389.jpeg"),
    ]
    return [path for path in candidates if path is not None]


def fill_media_containers(html_text: str, output_dir: Path) -> str:
    media_images = image_for_media_slots(output_dir)
    if not media_images:
        return html_text

    media_index = 0
    pattern = re.compile(
        r"(<ws-media-container\b(?P<attrs>[^>]*)>.*?</template>)(?P<children>.*?)</ws-media-container>",
        re.DOTALL,
    )

    def replacement(match: re.Match[str]) -> str:
        nonlocal media_index
        if media_index >= len(media_images):
            return match.group(0)

        attrs = match.group("attrs")
        position = "50% 50%"
        position_match = re.search(r'content-position="([^"]+)"', attrs)
        if position_match:
            position = position_match.group(1)

        if " " in position:
            left, top = position.split(" ", 1)
        else:
            left = top = "50%"

        open_part = match.group(1)
        open_part = re.sub(r'\sloaded=""', "", open_part, count=1)
        open_part = open_part.replace("<ws-media-container", '<ws-media-container loaded=""', 1)
        open_part = re.sub(
            r'<div class="ws-media-content-container(?: loaded)?" media-type="image"(?: style="[^"]*")?>',
            (
                '<div class="ws-media-content-container loaded" media-type="image" '
                f'style="left: {left}; top: {top}; width: 100%; height: 100%;">'
            ),
            open_part,
            count=1,
        )

        image = image_tag(media_images[media_index])
        media_index += 1
        return f"{open_part}\n{image}\n</ws-media-container>"

    return pattern.sub(replacement, html_text)


def local_export_styles() -> str:
    return """<style id="local-export-fixes">
ws-media-container img.ws-export-media-image {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center;
}

.ws-export-about-image {
  display: block;
  width: 100%;
  max-height: 620px;
  object-fit: cover;
}

.ws-export-about-copy {
  max-width: 720px;
}

.ws-export-accordion-header {
  font-weight: 600;
  letter-spacing: 0;
  text-transform: uppercase;
}

.ws-export-accordion-content {
  font-size: 16px;
  line-height: 1.6;
}

.ws-export-accordion-content p {
  margin: 0 0 0.5rem;
}

.ws-export-contact-item {
  text-align: center;
  padding: 1rem;
}

.ws-export-contact-item h3 {
  margin: 0 0 0.75rem;
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0;
  text-transform: uppercase;
}

.ws-export-contact-item a,
.ws-export-contact-item p {
  margin: 0;
  color: inherit;
  font-size: 16px;
  overflow-wrap: anywhere;
  text-decoration: none;
}

.ws-export-contact-form-wrap {
  width: 100%;
}

.ws-export-contact-form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 1fr);
  gap: 56px;
  align-items: start;
}

.ws-export-contact-form-copy {
  max-width: 640px;
}

.ws-export-contact-eyebrow {
  margin: 0 0 1rem;
  font-size: 16px;
  font-weight: 500;
  letter-spacing: 0;
  text-transform: uppercase;
}

.ws-export-contact-heading {
  margin: 0;
  font-family: var(--ws-primary-font-family);
  font-size: 54px;
  font-weight: 400;
  line-height: 1.35;
  letter-spacing: 0;
  text-transform: uppercase;
}

.ws-export-contact-lead {
  margin: 2rem 0 0;
  font-family: var(--ws-primary-font-family);
  font-size: 28px;
  line-height: 1.55;
}

.ws-export-static-form {
  border: 1px solid rgba(20, 22, 24, 0.16);
  padding: 32px;
  background: rgba(255, 255, 255, 0.28);
}

.ws-export-form-field {
  margin-bottom: 1.35rem;
}

.ws-export-form-field label {
  display: block;
  margin-bottom: 0.5rem;
  font-size: 18px;
  font-weight: 500;
}

.ws-export-form-field input,
.ws-export-form-field textarea {
  box-sizing: border-box;
  width: 100%;
  border: 1px solid rgba(20, 22, 24, 0.18);
  border-radius: 4px;
  padding: 16px;
  color: inherit;
  background: rgba(255, 255, 255, 0.35);
  font: inherit;
}

.ws-export-form-field textarea {
  min-height: 108px;
  resize: vertical;
}

.ws-export-required {
  color: #9a4e4a;
}

.ws-export-captcha {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: min(100%, 420px);
  border: 1px solid rgba(20, 22, 24, 0.16);
  border-radius: 4px;
  padding: 18px;
  margin-bottom: 1.8rem;
  background: rgba(255, 255, 255, 0.25);
}

.ws-export-captcha-check {
  width: 28px;
  height: 28px;
  border: 2px solid rgba(20, 22, 24, 0.35);
  border-radius: 4px;
  flex: 0 0 auto;
}

.ws-export-captcha-label {
  flex: 1 1 auto;
  font-size: 16px;
}

.ws-export-captcha-brand {
  text-align: center;
  font-size: 12px;
  line-height: 1.2;
}

.ws-export-submit {
  border: 0;
  padding: 18px 48px;
  color: #fff;
  background: #8d634a;
  font-family: var(--ws-primary-font-family);
  font-size: 18px;
  letter-spacing: 0;
  text-transform: uppercase;
  cursor: pointer;
}

@media (max-width: 900px) {
  .ws-export-contact-form-grid {
    grid-template-columns: 1fr;
    gap: 36px;
  }

  .ws-export-contact-heading {
    font-size: 40px;
  }

  .ws-export-contact-lead {
    font-size: 22px;
  }
}
</style>"""


def add_local_export_styles(html_text: str) -> str:
    if 'id="local-export-fixes"' in html_text:
        return html_text
    return html_text.replace("</head>", f"{local_export_styles()}</head>", 1)


def ws_column_template() -> str:
    return """<template shadowrootmode="open">
<style>
  [part="inner-wrapper"] {
    box-sizing: border-box;
  }
</style>

<div part="inner-wrapper">
  <slot></slot>
</div>
</template>"""


def rebuild_about_section(html_text: str, output_dir: Path) -> str:
    portrait = existing_image(output_dir, "IMG_2676.jpeg")
    if portrait is None:
        return html_text

    pattern = re.compile(
        r'(<ws-block\b[^>]*id="ws-block-about-with-media-CTios3UH"[^>]*><section\b[^>]*>).*?(</section>\s*</ws-block>)',
        re.DOTALL,
    )

    content = f"""
<div class="ws-container">
<ws-columns>
<ws-column class="col-10 col-lg-6">{ws_column_template()}
<div class="ws-block-content ws-export-about-copy">
<div class="ws-m-block-title-fit-wrapper">
<div class="ws-m-block-title-570">
<ws-text slot="block-title">
<h6 class=""><ws-color class="ws-custom-color" style="color: var(--text-color-1778930149401-1778930149400)">Om oss</ws-color></h6>
<h3 class="ws-fz-24">Hej! Det är jag som är Tindra Nordgren, grundare av STUDIOnour. Min passion har alltid varit att se potentialen i ett rum/hem och sedan förvandla detta till en hemtrevlig plats där man vill spendera sin tid. Jag tror helhjärtat på att färgsättning, belysning och textilier kan förändra hur vi mår. Jag startade upp STUDIOnour för att kunna hjälpa så många som möjligt, oavsett om du vill skapa en röd tråd i hemmet/jobbet eller sälja din nuvarande lägenhet.</h3>
<h3 class="ws-fz-24">Jag är nyligen utbildad till både inredningsdesigner och homestylist. Jag har även två diplom inom SketchUp och Feng Shui. Vilket gör att jag har goda kunskaper inom inredning och har fördjupat mig inom just Feng Shui, för att lättare veta hur man ska tänka för att få en så trivsam miljö som möjligt.</h3>
</ws-text>
</div>
</div>
</div>
</ws-column>
<ws-column class="col-10 col-lg-6 ws-block-media-content">{ws_column_template()}
<img alt="Tindra Nordgren" class="ws-export-about-image" src="{html_path(portrait)}">
</ws-column>
</ws-columns>
</div>"""

    return pattern.sub(lambda match: f"{match.group(1)}{content}{match.group(2)}", html_text, count=1)


def service_accordion_markup(title: str, body: str) -> str:
    return f"""
<div class="ws-export-accordion-header" slot="header">{html.escape(title)}</div>
<div class="ws-export-accordion-content" slot="content">
<p>{html.escape(body)}</p>
</div>"""


def rebuild_services_accordion(html_text: str) -> str:
    services = [
        (
            "FÄRG- & KONCEPTPAKETET",
            "Få komplett moodboard/schematisk moodboard digitalt, färgsättning och materialprover för att enkelt kunna genomföra designförändringar i din egen takt.",
        ),
        (
            "STYLING INFÖR FÖRSÄLJNING",
            "Professionell styling som hjälper din lägenhet att sticka ut och säljas snabbare. Nedan finns en länk på ett exempel på hur ett homestylinguppdrag kan se ut: Homestylinguppdrag.",
        ),
        (
            "FÖRSLAGSTYLING FÖR HEM OCH FÖRETAG",
            "Vi skapar inspirerande och välkomnande miljöer för både ditt hem och arbetsplats. Detta genom att ta fram allt från kompletta moodboards till produktlista.",
        ),
    ]

    section_pattern = re.compile(
        r'(<ws-block\b[^>]*id="ws-block-services-with-accordion-and-side-media-PEP4Y4Zd"[^>]*>.*?<ws-accordion\b[^>]*>)(?P<items>.*?)(</ws-accordion>.*?</ws-block>)',
        re.DOTALL,
    )
    item_pattern = re.compile(r"(<ws-accordion-item\b[^>]*>.*?</template>)(?:.*?)(</ws-accordion-item>)", re.DOTALL)

    def section_replacement(section_match: re.Match[str]) -> str:
        service_index = 0

        def item_replacement(item_match: re.Match[str]) -> str:
            nonlocal service_index
            if service_index >= len(services):
                return item_match.group(0)

            title, body = services[service_index]
            service_index += 1
            item_html = f"{item_match.group(1)}{service_accordion_markup(title, body)}{item_match.group(2)}"
            return item_html.replace('style="height: 108px;"', 'style="height: auto;"', 1)

        items = item_pattern.sub(item_replacement, section_match.group("items"))
        return f"{section_match.group(1)}{items}{section_match.group(3)}"

    return section_pattern.sub(section_replacement, html_text, count=1)


def contact_column(title: str, label: str, href: str | None = None) -> str:
    content = (
        f'<a class="ws-link" href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
        if href
        else f"<p>{html.escape(label)}</p>"
    )
    return f"""<ws-column class="col-10 col-sm-8 col-md-6 col-lg-4">{ws_column_template()}
<div class="ws-export-contact-item">
<h3>{html.escape(title)}</h3>
{content}
</div>
</ws-column>"""


def rebuild_contact_section(html_text: str) -> str:
    pattern = re.compile(
        r'(<ws-block\b[^>]*id="ws-block-features-with-icons-nZbnyVfy"[^>]*><section\b[^>]*>).*?(</section>\s*</ws-block>)',
        re.DOTALL,
    )

    content = f"""
<div class="ws-container">
<div class="ws-m-block-title-770">
<ws-text slot="block-title">
<h6 class="ws-fz-36"><ws-color class="ws-custom-color" style="color: var(--text-color-1778930300146-1778930300144)">KONTAKTA OSS</ws-color></h6>
</ws-text>
</div>
<div class="ws-m-feature" data-surface="1">
<ws-columns layout="3-columns" slot="block-columns">
{contact_column("E-post", "studionour.info@gmail.com", "mailto:studionour.info@gmail.com")}
{contact_column("TikTok", "https://www.tiktok.com/@studionour_", "https://www.tiktok.com/@studionour_")}
{contact_column("Instagram", "studionour", "https://www.instagram.com/studionour/")}
</ws-columns>
</div>
<div class="ws-m-button-group">
<ws-button-group slot="button-group">
</ws-button-group>
</div>
</div>"""

    return pattern.sub(lambda match: f"{match.group(1)}{content}{match.group(2)}", html_text, count=1)


def rebuild_contact_form_section(html_text: str) -> str:
    pattern = re.compile(
        r'(<ws-block\b[^>]*id="ws-block-custom-SaShBUZC"[^>]*><section\b[^>]*>).*?(</section>\s*</ws-block>)',
        re.DOTALL,
    )

    content = f"""
<div class="ws-container">
<div class="ws-export-contact-form-wrap" slot="custom-content">
<div class="ws-export-contact-form-grid">
<div class="ws-export-contact-form-copy">
<p class="ws-export-contact-eyebrow">KONTAKTA OSS</p>
<h2 class="ws-export-contact-heading">STARTA DIN RESA<br>MOT ETT VACKERT<br>HEM ELLER<br>ARBETSPLATS</h2>
<p class="ws-export-contact-lead">Oavsett om du vill skapa ett personligt boende eller förbättra din arbetsplats, finns vi här för att hjälpa dig.</p>
</div>
</div>
</div>"""

    return pattern.sub(lambda match: f"{match.group(1)}{content}{match.group(2)}", html_text, count=1)


def apply_local_asset_overrides(html_text: str, output_dir: Path) -> str:
    html_text = replace_known_local_images(html_text, output_dir)
    html_text = fill_media_containers(html_text, output_dir)
    html_text = rebuild_services_accordion(html_text)
    html_text = rebuild_about_section(html_text, output_dir)
    html_text = rebuild_contact_section(html_text)
    html_text = rebuild_contact_form_section(html_text)
    html_text = add_local_export_styles(html_text)
    return html_text


def write_resource(
    output_dir: Path,
    resource: Resource,
    alias_map: dict[str, Path],
    resources: list[Resource],
) -> None:
    destination = output_dir / resource.local_path
    destination.parent.mkdir(parents=True, exist_ok=True)

    if "css" in resource.mime or "javascript" in resource.mime:
        text = resource.data.decode(resource.encoding, "replace")
        text = replace_resource_urls(text, resource.local_path, alias_map, resources)
        if resource.local_path.name == "fonts.css":
            text = prune_external_font_faces(text)
        destination.write_text(text, encoding="utf-8")
    else:
        destination.write_bytes(resource.data)


def referenced_urls(text: str) -> set[str]:
    refs: set[str] = set()
    text = re.sub(
        r"\s+src=([\"'])/ws/(?:block-templates|globals)/[^\"']+\1",
        "",
        text,
    )
    attr_pattern = re.compile(
        r"""(?:href|src|poster|background-image|data-src|srcset)=["']([^"']+)["']""",
        re.IGNORECASE,
    )
    url_pattern = re.compile(r"""url\(([^)]+)\)""", re.IGNORECASE)

    for match in attr_pattern.finditer(text):
        raw = html.unescape(match.group(1)).strip()
        for part in raw.split(","):
            value = part.strip().split(" ", 1)[-1].strip()
            if value:
                refs.add(value)

    for match in url_pattern.finditer(text):
        refs.add(html.unescape(match.group(1)).strip().strip("\"'"))

    return {
        ref
        for ref in refs
        if ref
        and not ref.startswith(("data:", "#", "mailto:", "tel:", "true", "false"))
        and not ref.startswith(("assets/", "../assets/"))
    }


def is_intentional_external_link(ref: str) -> bool:
    parsed = urlparse(ref)
    return parsed.netloc.lower() in {"www.instagram.com", "instagram.com", "www.tiktok.com", "tiktok.com"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, default=Path("."))
    args = parser.parse_args()

    archive = plistlib.load(args.archive.open("rb"))
    page_resource, raw_resources = choose_page(archive)
    resources = collect_resources(raw_resources)
    alias_map = build_alias_map(resources)

    output_dir = args.out
    page_html = page_resource.get("WebResourceData", b"").decode(
        page_resource.get("WebResourceTextEncodingName") or "utf-8",
        "replace",
    )
    page_html = replace_resource_urls(page_html, Path("index.html"), alias_map, resources)
    page_html = cleanup_html(page_html)
    page_html = apply_local_asset_overrides(page_html, output_dir)

    for resource in resources:
        write_resource(output_dir, resource, alias_map, resources)

    (output_dir / "index.html").write_text(page_html, encoding="utf-8")

    missing = sorted(
        ref
        for ref in referenced_urls(page_html)
        if ref.startswith(("http://", "https://", "/ws/"))
        and not is_intentional_external_link(ref)
    )

    notes = [
        "# Static Sitebuilder Export",
        "",
        f"Source: `{args.archive.name}`",
        f"Page URL: `{page_resource.get('WebResourceURL', '')}`",
        f"Extracted resources: {len(resources)}",
        "",
        "Open `index.html` in a browser, or serve this directory with any static web server.",
        "",
        "Local preview: `python3 -m http.server 4173 --bind 127.0.0.1` and open http://127.0.0.1:4173/.",
    ]
    if missing:
        notes.extend(
            [
                "",
                "## External or Missing References",
                "",
                "These references were present in the HTML but were not embedded in the webarchive:",
                "",
                *[f"- `{ref}`" for ref in missing],
            ]
        )
    (output_dir / "README.md").write_text("\n".join(notes) + "\n", encoding="utf-8")

    print(f"Wrote {output_dir / 'index.html'}")
    print(f"Wrote {len(resources)} resources under {output_dir / 'assets'}")
    if missing:
        print(f"Found {len(missing)} external or missing references; see README.md")


if __name__ == "__main__":
    main()
