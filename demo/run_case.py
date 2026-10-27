"""Run one demo request against the running API and save the evidence.

Usage (server must be running):  python demo/run_case.py demo/test_a.txt
Writes demo/results/<case>.json and demo/results/<case>.docx and prints a readable summary.
"""
import json
import sys
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8000"


def main(case_file: str) -> None:
    path = Path(case_file)
    request_text = path.read_text(encoding="utf-8").strip()
    results = Path(__file__).parent / "results"
    results.mkdir(exist_ok=True)

    print(f"POST /agent  ({path.name}) - this can take a few minutes...")
    response = httpx.post(f"{BASE_URL}/agent", json={"request": request_text}, timeout=1800)
    body = response.json()
    (results / f"{path.stem}.json").write_text(json.dumps(body, indent=2), encoding="utf-8")

    print(f"\nHTTP {response.status_code} | status={body.get('status')} | fallback_used={body.get('fallback_used')}"
          f" | revisions={body.get('revisions_performed')} | model={body.get('model')}")
    print(f"Summary: {body.get('summary')}")
    print("\nPlan:")
    for task in body.get("plan", []):
        print(f"  {task['id']}. [{task['status']:<9}] {task['handler']:<20} {task['title']}")
    print(f"\nAssumptions ({len(body.get('assumptions', []))}):")
    for item in body.get("assumptions", []):
        print(f"  - [{item['category']}] {item['statement']}")
    print(f"\nOpen questions ({len(body.get('open_questions', []))}):")
    for question in body.get("open_questions", []):
        print(f"  - {question}")
    review = body.get("review")
    if review:
        print(f"\nReview: score={review['score']}  gaps={len(review['identified_gaps'])}  remaining={len(review['remaining_issues'])}")
        for issue in review["remaining_issues"]:
            print(f"  - remaining: {issue}")
    for warning in body.get("warnings", []):
        print(f"  ! warning: {warning}")

    if body.get("download_url"):
        download = httpx.get(BASE_URL + body["download_url"])
        out = results / f"{path.stem}.docx"
        out.write_bytes(download.content)
        print(f"\nDownload HTTP {download.status_code}: saved {out} ({len(download.content)} bytes)")
    else:
        print(f"\nNo document. Error: {body.get('error')}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python demo/run_case.py demo/test_a.txt")
    main(sys.argv[1])
