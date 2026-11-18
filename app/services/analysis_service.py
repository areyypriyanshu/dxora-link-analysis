"""Turn a URL into the five-section analysis: extract -> LLM -> validate -> normalise."""

import json
import logging

import anyio
import httpx
from pydantic import ValidationError

from ..schemas import LinkAnalysisResponse, LLMOutput
from .competitor_service import normalize_competitor_analysis
from .llm_service import LLMError, LLMService
from .website_extractor import (
    ExtractedContent,
    FetchError,
    build_prompt_document,
    extract_content,
    fetch_html,
)

logger = logging.getLogger(__name__)

MIN_CONTENT_CHARS = 200

OUTPUT_SCHEMA = """{
  "brand": {"name": "string", "category": "string", "summary": "string"},
  "analysis": {
    "competitor_analysis": {
      "brand_overview": "string",
      "competitors": [
        {"name": "string", "website": "string or null", "strengths": ["string"], "weaknesses": ["string"]}
      ],
      "strengths": ["competitive advantages of the analysed brand"],
      "opportunities": ["opportunities to improve market positioning"],
      "disclaimer": "one sentence stating competitors are AI-generated suggestions"
    },
    "target_search_queries": [
      {"query": "string", "platforms": ["Google|Facebook|Instagram|LinkedIn|X"],
       "intent": "string", "business_relevance": "string", "target_audience": "string"}
    ],
    "seo_improvements": {
      "primary_keyword": "string",
      "secondary_keywords": ["string"],
      "long_tail_keywords": ["string"],
      "recommended_title": "string, <= 60 chars, contains the primary keyword",
      "meta_description": "string, <= 155 chars, contains the primary keyword",
      "supporting_content": ["string"]
    },
    "top_services": [
      {"service_name": "string", "description": "string", "target_industry": "string",
       "target_customer": "string", "main_benefit": "string", "source": "website|inferred"}
    ],
    "target_audience": {
      "primary_audience": ["string"],
      "secondary_audience": ["string"],
      "industries": ["string"],
      "business_types": ["string"],
      "customer_profiles": ["string"],
      "location_focus": "string or null",
      "customer_needs": ["string"],
      "potential_interests": ["string"],
      "recommended_segments": ["string"]
    }
  }
}"""

TASK_INSTRUCTIONS = """Analyse the web page below and produce exactly five sections.

1. competitor_analysis: identify the brand, its business category and overview, then 3-5 real
   competitors with their strengths and weaknesses or gaps; list the brand's competitive
   advantages and opportunities to improve its market positioning.
2. target_search_queries: exactly 5 realistic queries potential customers would use, each with
   the platforms where it fits (from Google, Facebook, Instagram, LinkedIn, X), the search
   intent, business relevance and target audience. Do not state search volumes.
3. seo_improvements: primary keyword, secondary keywords, long-tail keywords, a recommended SEO
   title (<= 60 chars) and meta description (<= 155 chars) containing the primary keyword, plus
   supporting content ideas (landing pages, FAQ, blog topics, location pages...).
4. top_services: exactly 5 services. Mark source "website" when the page states or directly
   implies the service, "inferred" when it is a reasonable suggestion.
5. target_audience: primary/secondary audiences, industry segments, business types, customer
   profiles, location focus if the page states one, customer needs and challenges, potential
   interests, and recommended audience segments for marketing.

Never fabricate search volumes, traffic or ranking statistics. Competitor information is an
AI-generated suggestion based on general market knowledge; state that in the disclaimer field.
"""

PROMPT_TEMPLATE = """{task}

Respond with a single JSON object matching this schema exactly (same keys, same nesting):
{schema}

--- PAGE CONTENT ---
{document}
"""


def build_analysis_prompt(content: ExtractedContent) -> str:
    return PROMPT_TEMPLATE.format(
        task=TASK_INSTRUCTIONS,
        schema=OUTPUT_SCHEMA,
        document=build_prompt_document(content),
    )


def _repair_prompt(original: str, previous: dict, error: ValidationError) -> str:
    return (
        original
        + "\n\n--- PREVIOUS RESPONSE (failed validation) ---\n"
        + json.dumps(previous)[:6000]
        + "\n\nValidation errors were:\n"
        + str(error)[:4000]
        + "\n\nReturn the corrected, complete JSON object only."
    )


async def analyze_website(url: str, client: httpx.AsyncClient) -> LinkAnalysisResponse:
    final_url, html = await fetch_html(url, client)
    content = await anyio.to_thread.run_sync(
        extract_content, html, url, final_url, abandon_on_cancel=True
    )
    if len(content.text) < MIN_CONTENT_CHARS:
        raise FetchError("The page did not contain enough readable text to analyse")
    logger.info("Extracted %d characters from %s", len(content.text), final_url)

    prompt = build_analysis_prompt(content)
    service = LLMService()

    data = await service.generate_json(prompt)
    try:
        output = LLMOutput.model_validate(data)
    except ValidationError as first_error:
        logger.warning("LLM output failed validation, retrying once: %s", first_error)
        data = await service.generate_json(_repair_prompt(prompt, data, first_error))
        try:
            output = LLMOutput.model_validate(data)
        except ValidationError as second_error:
            raise LLMError("LLM output did not match the required schema after a repair attempt") from second_error

    analysis = normalize_competitor_analysis(output.analysis)
    return LinkAnalysisResponse(website=final_url, brand=output.brand, analysis=analysis)
