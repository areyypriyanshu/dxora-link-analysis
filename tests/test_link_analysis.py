"""Offline tests: no network calls, no LLM calls, no API key required."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.schemas import Analysis, LinkAnalysisResponse
from app.services import analysis_service, website_extractor
from app.services.competitor_service import normalize_competitor_analysis
from app.services.llm_service import LLMConfigError, LLMError
from app.services.website_extractor import FetchError, build_prompt_document, extract_content
from app.utils.validators import UnsafeURLError, validate_public_url

SAMPLE_HTML = """<html><head>
<title>Acme Robotics | Industrial Automation</title>
<meta name="description" content="Acme builds robotic arms for factories.">
<meta property="og:site_name" content="Acme Robotics">
</head><body>
<header><nav><a href="/products">Products</a><a href="/about">About</a></nav></header>
<main>
<h1>Industrial robotics</h1>
<h2>Our services</h2>
<p>We design, install and maintain robotic assembly lines for automotive and electronics manufacturers.</p>
<p>Founded in 2005, serving clients across Europe.</p>
</main></body></html>"""

LONG_HTML = (
    "<html><head><title>Acme Robotics</title></head><body><main><h1>Acme Robotics</h1><p>"
    + "Robotic assembly lines for automotive and electronics manufacturers. " * 6
    + "</p></main></body></html>"
)

BRAND = {
    "name": "Acme Robotics",
    "category": "Industrial Automation",
    "summary": "Robotic assembly lines for factories.",
}

ANALYSIS = {
    "competitor_analysis": {
        "brand_overview": "Acme Robotics builds robotic assembly lines for mid-size factories.",
        "competitors": [
            {
                "name": "KUKA",
                "website": "https://www.kuka.com",
                "strengths": ["global brand"],
                "weaknesses": ["high price point"],
            }
        ],
        "strengths": ["focused on mid-size factories"],
        "opportunities": ["publish case studies for SEO"],
        "disclaimer": "",
    },
    "target_search_queries": [
        {
            "query": "robotic assembly line supplier",
            "platforms": ["Google", "LinkedIn"],
            "intent": "Service search",
            "business_relevance": "core offering",
            "target_audience": "plant managers",
        }
    ],
    "seo_improvements": {
        "primary_keyword": "industrial robotics",
        "secondary_keywords": ["assembly line automation"],
        "long_tail_keywords": ["robotic assembly line for small factories"],
        "recommended_title": "Industrial Robotics for Mid-Size Factories | Acme",
        "meta_description": "Acme Robotics designs and maintains robotic assembly lines.",
        "supporting_content": ["FAQ page"],
    },
    "top_services": [
        {
            "service_name": "Assembly line design",
            "description": "Custom robotic lines.",
            "target_industry": "automotive",
            "target_customer": "mid-size manufacturers",
            "main_benefit": "higher throughput",
            "source": "website",
        }
    ],
    "target_audience": {
        "primary_audience": ["mid-size manufacturers"],
        "secondary_audience": ["system integrators"],
        "industries": ["automotive", "electronics"],
        "business_types": ["manufacturer"],
        "customer_profiles": ["plant manager"],
        "location_focus": "Europe",
        "customer_needs": ["automation expertise"],
        "potential_interests": ["predictive maintenance"],
        "recommended_segments": ["automotive plants"],
    },
}

LLM_OUTPUT = {"brand": BRAND, "analysis": ANALYSIS}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/admin",
        "http://10.0.0.5:8080/",
        "http://192.168.1.10/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
    ],
)
def test_blocks_private_and_internal_addresses(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "http://metadata.google.internal/",
        "http://printer.local/",
        "http://db.internal/",
    ],
)
def test_blocks_local_hostnames(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    ["", "example.com", "ftp://example.com/file", "file:///etc/passwd", "http://user:pass@example.com/"],
)
def test_blocks_malformed_or_non_http_urls(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://100.64.0.1/",
        "http://100.127.255.254/",
        "http://198.18.0.1/",
        "http://[fec0::1]/",
    ],
)
def test_blocks_non_global_special_ranges(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


class _FakeStream:
    def __init__(self, address):
        self._address = address

    def get_extra_info(self, info):
        return self._address if info == "server_addr" else None


def _response_with_peer(address):
    return SimpleNamespace(extensions={"network_stream": _FakeStream(address)})


def test_extractor_rejects_private_peer_address():
    with pytest.raises(UnsafeURLError):
        website_extractor._ensure_public_peer(_response_with_peer(("10.0.0.5", 443)))


def test_extractor_accepts_public_peer_address():
    website_extractor._ensure_public_peer(_response_with_peer(("1.1.1.1", 443)))


def test_extract_content_parses_page():
    content = extract_content(SAMPLE_HTML, "https://acme.example", "https://acme.example")
    assert content.title == "Acme Robotics | Industrial Automation"
    assert content.site_name == "Acme Robotics"
    assert "robotic arms" in content.meta_description
    assert "Industrial robotics" in content.headings
    assert "Our services" in content.headings
    assert "Products" in content.nav_links
    assert "robotic assembly lines" in content.text
    assert "Page URL: https://acme.example" in build_prompt_document(content)


def test_competitor_normalisation_dedupes_and_adds_disclaimer():
    data = json.loads(json.dumps(ANALYSIS))
    data["competitor_analysis"]["competitors"] = [
        {"name": name, "strengths": [], "weaknesses": []}
        for name in ["Alpha", "alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    ]
    result = normalize_competitor_analysis(Analysis.model_validate(data))
    names = [competitor.name for competitor in result.competitor_analysis.competitors]
    assert names == ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    assert result.competitor_analysis.disclaimer


def test_root_redirects_to_docs(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_missing_url_returns_structured_422(client):
    response = client.post("/link-analysis", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "invalid_request"


def test_private_url_returns_400(client):
    response = client.post("/link-analysis", json={"url": "http://127.0.0.1/"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_url"


def test_oversized_body_returns_413(client):
    response = client.post(
        "/link-analysis",
        json={"url": "https://acme.example", "padding": "x" * 20_000},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_success_response(client, monkeypatch):
    monkeypatch.setattr(main_module, "validate_public_url", lambda url: url)

    async def fake_analyze(url, http_client):
        return LinkAnalysisResponse.model_validate({"website": url, **LLM_OUTPUT})

    monkeypatch.setattr(main_module, "analyze_website", fake_analyze)

    response = client.post("/link-analysis", json={"url": "https://acme.example"})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["brand"]["name"] == "Acme Robotics"
    assert set(body["analysis"]) == {
        "competitor_analysis",
        "target_search_queries",
        "seo_improvements",
        "top_services",
        "target_audience",
    }


@pytest.mark.parametrize(
    "error,status,code",
    [
        (FetchError("Website returned HTTP 404"), 502, "fetch_failed"),
        (LLMError("provider exploded"), 502, "llm_failed"),
        (LLMConfigError("GEMINI_API_KEY is not set"), 503, "llm_not_configured"),
    ],
)
def test_upstream_failures_map_to_structured_errors(client, monkeypatch, error, status, code):
    monkeypatch.setattr(main_module, "validate_public_url", lambda url: url)

    async def boom(url, http_client, _error=error):
        raise _error

    monkeypatch.setattr(main_module, "analyze_website", boom)

    response = client.post("/link-analysis", json={"url": "https://acme.example"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_llm_output_is_repaired_once(monkeypatch):
    async def fake_fetch(url, http_client):
        return url, LONG_HTML

    class RepairingLLM:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return {"brand": {}, "analysis": {}}
            return LLM_OUTPUT

    monkeypatch.setattr(analysis_service, "fetch_html", fake_fetch)
    monkeypatch.setattr(analysis_service, "LLMService", RepairingLLM)

    result = asyncio.run(analysis_service.analyze_website("https://acme.example", None))

    assert result.brand.name == "Acme Robotics"
    assert result.analysis.competitor_analysis.disclaimer


def test_llm_output_failure_after_repair_raises(monkeypatch):
    async def fake_fetch(url, http_client):
        return url, LONG_HTML

    class BrokenLLM:
        async def generate_json(self, prompt):
            return {"brand": {}}

    monkeypatch.setattr(analysis_service, "fetch_html", fake_fetch)
    monkeypatch.setattr(analysis_service, "LLMService", BrokenLLM)

    with pytest.raises(LLMError):
        asyncio.run(analysis_service.analyze_website("https://acme.example", None))
