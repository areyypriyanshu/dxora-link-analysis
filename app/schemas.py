"""Pydantic models for the request, the LLM output, and the API response."""

from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["Google", "Facebook", "Instagram", "LinkedIn", "X"]


class LinkAnalysisRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=4,
        max_length=2048,
        examples=["https://example.com"],
        description="Public website URL to analyse.",
    )


class BrandInfo(BaseModel):
    name: str
    category: str
    summary: str


class Competitor(BaseModel):
    name: str
    website: str | None = None
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)


class CompetitorAnalysis(BaseModel):
    brand_overview: str
    competitors: list[Competitor] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    disclaimer: str = ""


class SearchQueryInsight(BaseModel):
    query: str
    platforms: list[Platform] = Field(default_factory=list)
    intent: str
    business_relevance: str
    target_audience: str


class SEOImprovements(BaseModel):
    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list)
    long_tail_keywords: list[str] = Field(default_factory=list)
    recommended_title: str
    meta_description: str
    supporting_content: list[str] = Field(default_factory=list)


class ServiceInsight(BaseModel):
    service_name: str
    description: str
    target_industry: str
    target_customer: str
    main_benefit: str
    source: Literal["website", "inferred"] = "inferred"


class TargetAudience(BaseModel):
    primary_audience: list[str] = Field(default_factory=list)
    secondary_audience: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    business_types: list[str] = Field(default_factory=list)
    customer_profiles: list[str] = Field(default_factory=list)
    location_focus: str | None = None
    customer_needs: list[str] = Field(default_factory=list)
    potential_interests: list[str] = Field(default_factory=list)
    recommended_segments: list[str] = Field(default_factory=list)


class Analysis(BaseModel):
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQueryInsight] = Field(default_factory=list)
    seo_improvements: SEOImprovements
    top_services: list[ServiceInsight] = Field(default_factory=list)
    target_audience: TargetAudience


class LLMOutput(BaseModel):
    """Shape the raw LLM JSON is validated against before assembly."""

    brand: BrandInfo
    analysis: Analysis


class LinkAnalysisResponse(BaseModel):
    success: bool = True
    website: str
    brand: BrandInfo
    analysis: Analysis


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail
