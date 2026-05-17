"""
=============================================================
chat_service/intent_classifier.py
=============================================================
"""
import os
from groq import Groq
from dotenv import load_dotenv
load_dotenv()

_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))

REPORT_KEYWORDS = [
    "show", "list", "total", "sum", "count", "how many",
    "report", "summary", "top", "compare", "between",
    "this month", "last month", "this year", "last year",
    "highest", "lowest", "average", "trend", "breakdown",
    "export", "download", "chart", "graph", "pivot"
]

def classify_intent(question: str) -> str:
    """
    Classify question as 'report' or 'knowledge'.
    Fast keyword check first, LLM only if ambiguous.
    Returns: 'report' | 'knowledge'
    """
    q_lower = question.lower()

    # Fast path: keyword match
    if any(kw in q_lower for kw in REPORT_KEYWORDS):
        return 'report'

    # LLM for ambiguous cases
    try:
        resp = _groq.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{
                "role": "user",
                "content": (
                    f"Classify this question as REPORT or KNOWLEDGE.\n"
                    f"REPORT = wants data, numbers, records from DB.\n"
                    f"KNOWLEDGE = wants explanation, help, instructions.\n"
                    f"Question: {question}\n"
                    f"Answer with one word only: REPORT or KNOWLEDGE"
                )
            }],
            max_tokens=5, temperature=0
        )
        answer = resp.choices[0].message.content.strip().upper()
        return 'report' if 'REPORT' in answer else 'knowledge'
    except:
        return 'knowledge'  # safe fallback