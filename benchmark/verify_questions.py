import json,re,sys,unicodedata,pypdf
def norm(t): return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',t)).strip()
r=pypdf.PdfReader(sys.argv[1]); d=json.load(open(sys.argv[2], encoding="utf-8")); man=json.load(open(sys.argv[3], encoding="utf-8"))
sec={p["pdf_page"]:p["section"] for p in man["sources"][0]["pages"]}
inc={p["pdf_page"] for p in man["sources"][0]["pages"] if p["included"]}
bad=0
for q in d["questions"]:
    pairs=[(q["gold_page"],q["evidence_quote"],False)]+[(a["gold_page"],a["evidence_quote"],a.get("outside_filter",False)) for a in q["alternative_evidence"]]
    for p,e,outside in pairs:
        ok = norm(e) in norm(r.pages[p-1].extract_text()) and p in inc
        if q["filter"] and not outside: ok = ok and sec[p]==q["filter"]["section"]
        if not ok: bad+=1; print("FAIL",q["id"],p,e[:60])
print("checked",len(d["questions"]),"questions; failures:",bad)