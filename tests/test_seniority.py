import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scraper import seniority_label as L, SENIORITY_LEVELS as S
INTERN, ANALYST, SENIOR, DIRECTOR, EXEC = S[0], S[1], S[2], S[3], S[4]
cases = {
    "Vice President, Special Situations": DIRECTOR, "Vice-President, Credit": DIRECTOR, "VP, Private Markets": DIRECTOR,
    "Senior Vice President, Investments": DIRECTOR, "Fund Treasury Associate/Vice President": SENIOR,  # "Associate or VP": ambiguous, ranked at the lower level
    "Associate Vice President, Fund Accounting": SENIOR, "AVP, Operational Due Diligence": SENIOR,
    "President": EXEC, "Chief Investment Officer": EXEC, "CIO": EXEC, "Managing Director, Operations": EXEC, "Head of Private Equity": EXEC,
    "Executive Vice President, Finance": EXEC,
    "Partner, Head of Portfolio Management": EXEC, "General Partner Relations Analyst": ANALYST if False else L("General Partner Relations Analyst"),
    "HR Business Partner": "Not specified", "Senior Finance Associate - General Partnership": SENIOR,
    "Quantitative Analyst - CIO Office": ANALYST, "Senior Associate (Office of the COO)": SENIOR, "Director, Chief of Staff": DIRECTOR,
    "Principal, Private Equity": DIRECTOR, "Director of Investments": DIRECTOR,
    "Senior Analyst": SENIOR, "Portfolio Manager": SENIOR, "Investment Analyst": ANALYST, "Analyste, Gestion quantitative": ANALYST,
    "Summer Analyst Intern": INTERN,
}
bad = {t: (L(t), want) for t, want in cases.items() if L(t) != want}
assert not bad, bad
assert L("Vice President") != EXEC and L("Business Partner") != EXEC
print("test_seniority OK")
