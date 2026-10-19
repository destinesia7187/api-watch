"""Verify the real dashboard assets work with its same-origin security policy.

These are resource-delivery checks, not snapshots of visual CSS declarations.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stylesheets = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and "stylesheet" in attributes.get("rel", "").split():
            self.stylesheets.append(attributes.get("href", ""))
        elif tag == "script" and attributes.get("src"):
            self.scripts.append(attributes["src"])


def same_origin_path(reference, parent="http://testserver/"):
    target = urlsplit(urljoin(parent, reference))
    assert target.scheme == "http" and target.netloc == "testserver", (
        "Dashboard scripts, styles and fonts must be self-hosted to preserve CSP and privacy."
    )
    assert target.path.startswith("/static/"), "A dashboard asset must be served by the static mount."
    return target.path + ("?" + target.query if target.query else "")


def dashboard_assets(client):
    index = client.get("/")
    assert index.status_code == 200
    references = AssetReferences()
    references.feed(index.text)
    return index, references


def test_dashboard_referenced_styles_and_scripts_are_served_locally(client):
    _, references = dashboard_assets(client)
    assert references.stylesheets and references.scripts
    for reference in references.stylesheets:
        response = client.get(same_origin_path(reference))
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
        assert response.content
    for reference in references.scripts:
        response = client.get(same_origin_path(reference))
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert response.content


def test_dashboard_fonts_are_valid_local_resources_allowed_by_csp(client):
    index, references = dashboard_assets(client)
    directives = {}
    for directive in index.headers["content-security-policy"].split(";"):
        parts = directive.split()
        if parts:
            directives[parts[0]] = parts[1:]
    font_sources = directives.get("font-src", directives.get("default-src", []))
    assert "'self'" in font_sources, "The dashboard CSP must permit its self-hosted fonts."

    families, font_urls = set(), set()
    for reference in references.stylesheets:
        stylesheet_path = same_origin_path(reference)
        css = client.get(stylesheet_path).text
        for face in re.findall(r"@font-face\s*\{([^}]+)\}", css, flags=re.I | re.S):
            family = re.search(r"font-family\s*:\s*([^;]+)", face, flags=re.I)
            assert family, "Every font face must name a font family."
            families.add(family.group(1).strip().strip("\"'"))
            sources = re.findall(r"url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)", face, flags=re.I)
            assert sources, "A self-hosted font face must reference an actual font resource."
            for source in sources:
                font_urls.add(same_origin_path(source, "http://testserver" + stylesheet_path))

    assert {"Inter", "JetBrains Mono"} <= families, "The supplied design requires both UI and code fonts."
    assert font_urls
    for path in font_urls:
        response = client.get(path)
        assert response.status_code == 200, f"Missing dashboard font resource: {path}"
        assert response.content[:4] in {b"wOF2", b"wOFF", b"OTTO", b"\x00\x01\x00\x00"}, (
            "A successful font response must be real font data, not an HTML fallback or error page."
        )
        assert "text/html" not in response.headers["content-type"]
