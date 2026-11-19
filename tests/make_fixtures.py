"""Build tests/fixtures/raw_items.json from Apify CSV exports (flattened list columns -> lists).
Run once; the JSON is committed so the comparison test never depends on the network."""
import csv, json, sys, re, collections
csv.field_size_limit(10**9)
KEEP = ["title","organization","url","date_posted","description_text","employment_type","domain_derived",
        "locations_derived","locations_raw","countries_derived","ai_experience_level","ai_employment_type"]
def load(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))
    out = []
    for r in rows:
        lists = collections.defaultdict(list)
        for k, v in r.items():
            m = re.match(r"^(.*)/(\d+)$", k)
            if m and v != "":
                lists[m.group(1)].append(v)
        item = {}
        for k in KEEP:
            if k in lists: item[k] = lists[k]
            elif r.get(k, "") != "": item[k] = r[k]
        if "description_text" in item: item["description_text"] = item["description_text"][:700]
        out.append(item)
    return out
items = []
for p in sys.argv[2:]:
    items += load(p)
json.dump(items, open(sys.argv[1], "w"), ensure_ascii=False)
print(len(items), "items")
