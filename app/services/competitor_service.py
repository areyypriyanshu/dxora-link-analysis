"""Enforce quality rules on the competitor section of the LLM output."""

from ..schemas import Analysis

MAX_COMPETITORS = 5

DISCLAIMER = (
    "Competitor details are AI-generated suggestions derived from the analysed page content "
    "and general market knowledge. Verify facts independently before acting on them."
)


def normalize_competitor_analysis(analysis: Analysis) -> Analysis:
    competitors = []
    seen: set[str] = set()
    for competitor in analysis.competitor_analysis.competitors:
        key = competitor.name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        competitors.append(competitor)

    competitor_analysis = analysis.competitor_analysis.model_copy(
        update={
            "competitors": competitors[:MAX_COMPETITORS],
            "disclaimer": analysis.competitor_analysis.disclaimer.strip() or DISCLAIMER,
        }
    )
    return analysis.model_copy(update={"competitor_analysis": competitor_analysis})
