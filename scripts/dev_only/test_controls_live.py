import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ai.provider import GeminiProvider  # noqa: E402
from app.ai.summarizer import analyze_article  # noqa: E402
from test_false_positive_benchmark import CONTROL_CASES  # noqa: E402

async def main():
    print("Running 3 Control Cases on Live Google Gemini...")
    provider = GeminiProvider(model="gemini-3-flash-preview")
    results = []
    for c in CONTROL_CASES:
        res = await analyze_article(c["title"], c["body"], c["source"], provider=provider)
        print(f"[{c['id']}] {c['name']}")
        print(f"  is_injection_attempt: {res.is_injection_attempt}")
        print(f"  Headline: {res.headline_fa}")
        print(f"  Categories: {res.categories}")
        print(f"  Verification Status: {res.verification_status}")
        results.append({
            "id": c["id"],
            "name": c["name"],
            "is_injection_attempt": res.is_injection_attempt,
            "headline_fa": res.headline_fa,
            "categories": res.categories,
            "verification_status": res.verification_status,
        })
    Path("scripts/controls_live_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Controls test complete.")

if __name__ == "__main__":
    asyncio.run(main())
