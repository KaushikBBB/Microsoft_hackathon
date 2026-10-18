"""Publish the verifier's data to Microsoft Fabric and build the Power BI dashboard.

  1. workspace "Ghost Listing Verifier" on the first active Fabric capacity (trial or F-SKU)
  2. Lakehouse lh_ghost: every ../fabric_export/*.csv -> typed Parquet -> Files/ -> a Delta table
  3. Direct Lake semantic model "Ghost Listing Model" with the dashboard's measures
  4. Power BI report "Directory Integrity Dashboard"

Re-running updates everything in place. Sign-in comes from the Azure CLI (az login).
Usage:  uv run python fabric_export.py && uv run python fabric_publish.py
"""
import base64
import io
import json
import subprocess
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import requests

API = "https://api.fabric.microsoft.com/v1"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
WORKSPACE, LAKEHOUSE = "Ghost Listing Verifier", "lh_ghost"
MODEL, REPORT = "Ghost Listing Model", "Directory Integrity Dashboard"
SRC = Path(__file__).parent.parent / "fabric_export"
MODEL_TABLES = ["run_outcomes", "claim_flags"]   # what the dashboard reads; the lakehouse gets all tables


def token(resource: str) -> str:
    return subprocess.run(["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken",
                           "-o", "tsv"], capture_output=True, text=True, check=True).stdout.strip()


FABRIC = {"Authorization": f"Bearer {token('https://api.fabric.microsoft.com')}"}
STORAGE = {"Authorization": f"Bearer {token('https://storage.azure.com')}"}


def call(method: str, path: str, body=None) -> dict:
    """Fabric REST call that waits for long-running operations."""
    r = requests.request(method, API + path, headers=FABRIC, json=body)
    if r.status_code == 202 and "Location" in r.headers:
        while True:
            time.sleep(int(r.headers.get("Retry-After", 3)))
            r = requests.get(r.headers["Location"], headers=FABRIC)
            if r.status_code == 200 and r.json().get("status") in (None, "Succeeded"):
                result = r.headers.get("Location")
                return requests.get(result, headers=FABRIC).json() if result else r.json()
            if r.status_code >= 400 or r.json().get("status") == "Failed":
                raise RuntimeError(f"{method} {path}: {r.text}")
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text}")
    return r.json() if r.content else {}


def find(items: list, name: str):
    return next((i for i in items if i["displayName"] == name), None)


def workspace() -> str:
    ws = find(call("GET", "/workspaces")["value"], WORKSPACE)
    if ws:
        return ws["id"]
    caps = [c for c in call("GET", "/capacities")["value"]
            if c["state"] == "Active" and c["sku"].startswith("F")]  # F-SKU or trial (FTL); PPU can't host a lakehouse
    if not caps:
        raise SystemExit("No active Fabric capacity: start a Fabric trial or create/resume an F-SKU capacity.")
    print(f"capacity: {caps[0]['displayName']} ({caps[0]['sku']})")
    return call("POST", "/workspaces", {"displayName": WORKSPACE, "capacityId": caps[0]["id"]})["id"]


def lakehouse(ws: str) -> dict:
    lh = find(call("GET", f"/workspaces/{ws}/lakehouses")["value"], LAKEHOUSE)
    if not lh:
        call("POST", f"/workspaces/{ws}/lakehouses", {"displayName": LAKEHOUSE})
    for _ in range(40):  # the SQL endpoint is provisioned a little after the lakehouse
        lh = find(call("GET", f"/workspaces/{ws}/lakehouses")["value"], LAKEHOUSE)
        if lh and (lh.get("properties", {}).get("sqlEndpointProperties") or {}).get("id"):
            return lh
        time.sleep(5)
    raise RuntimeError("lakehouse SQL endpoint not ready")


def to_parquet(csv_path: Path) -> tuple[bytes, pa.Schema]:
    t = pacsv.read_csv(csv_path)
    # all-empty columns come back as null type, which Delta can't store; Delta wants microsecond timestamps
    def fix(f):
        if pa.types.is_null(f.type):
            return pa.string()
        if pa.types.is_timestamp(f.type):
            return pa.timestamp("us", tz=f.type.tz)
        return f.type
    t = t.cast(pa.schema([pa.field(f.name, fix(f)) for f in t.schema]))
    buf = io.BytesIO()
    pq.write_table(t, buf)
    return buf.getvalue(), t.schema


def upload(ws: str, lh: str, name: str, data: bytes):
    url = f"{ONELAKE}/{ws}/{lh}/Files/fabric_export/{name}"
    for method, params, body in (("PUT", {"resource": "file"}, None),
                                 ("PATCH", {"action": "append", "position": 0}, data),
                                 ("PATCH", {"action": "flush", "position": len(data)}, None)):
        r = requests.request(method, url, params=params, headers=STORAGE, data=body)
        if r.status_code >= 400:
            raise RuntimeError(f"OneLake {method} {name}: {r.status_code} {r.text}")


TMDL_TYPE = [(pa.types.is_boolean, "boolean"), (pa.types.is_integer, "int64"), (pa.types.is_floating, "double"),
             (pa.types.is_decimal, "decimal"), (pa.types.is_temporal, "dateTime")]
MEASURES = {
    "run_outcomes": {
        "Exposure Found": ("SUM(run_outcomes[exposure_per_month])", "\\$#,0"),
        "Exposure Removed": ("CALCULATE(SUM(run_outcomes[exposure_per_month]), "
                             "run_outcomes[final] IN {\"suppress_listing\", \"set_not_accepting\"})", "\\$#,0"),
        "Exposure Open": ("CALCULATE(SUM(run_outcomes[exposure_per_month]), "
                          "run_outcomes[final] IN {\"pending_review\", \"not_called\"})", "\\$#,0"),
        "Listings Checked": ("COUNTROWS(run_outcomes)", "#,0"),
        "Cleared by Claims": ("CALCULATE(COUNTROWS(run_outcomes), run_outcomes[source] = \"claims\")", "#,0"),
        "Calls Placed": ("CALCULATE(COUNTROWS(run_outcomes), run_outcomes[source] = \"call\")", "#,0"),
        "Needs Review": ("CALCULATE(COUNTROWS(run_outcomes), run_outcomes[final] = \"pending_review\")", "#,0"),
    },
    "claim_flags": {
        "Open Flagged Claims": ("CALCULATE(COUNTROWS(claim_flags), claim_flags[status] = \"open\")", "#,0"),
        "Open Flagged $": ("CALCULATE(SUM(claim_flags[amount]), claim_flags[status] = \"open\")", "\\$#,0"),
    },
}


def table_tmdl(name: str, schema: pa.Schema) -> str:
    lines = [f"table {name}", ""]
    for f in schema:
        dtype = next((t for test, t in TMDL_TYPE if test(f.type)), "string")
        lines += [f"\tcolumn {f.name}", f"\t\tdataType: {dtype}", f"\t\tsourceColumn: {f.name}",
                  "\t\tsummarizeBy: none", ""]
    for m, (expr, fmt) in MEASURES.get(name, {}).items():
        lines += [f"\tmeasure '{m}' = {expr}", f"\t\tformatString: {fmt}", ""]
    lines += [f"\tpartition {name} = entity", "\t\tmode: directLake", "\t\tsource",
              f"\t\t\tentityName: {name}", "\t\t\tschemaName: dbo", "\t\t\texpressionSource: DatabaseQuery", ""]
    return "\n".join(lines)


def part(path: str, text: str) -> dict:
    return {"path": path, "payload": base64.b64encode(text.encode()).decode(), "payloadType": "InlineBase64"}


def upsert(ws: str, kind: str, name: str, parts: list) -> str:
    """Create the item, or replace its definition if it exists."""
    item = find(call("GET", f"/workspaces/{ws}/{kind}")["value"], name)
    if item:
        call("POST", f"/workspaces/{ws}/{kind}/{item['id']}/updateDefinition", {"definition": {"parts": parts}})
        return item["id"]
    call("POST", f"/workspaces/{ws}/{kind}", {"displayName": name, "definition": {"parts": parts}})
    return find(call("GET", f"/workspaces/{ws}/{kind}")["value"], name)["id"]


def semantic_model(ws: str, sql: dict, schemas: dict) -> str:
    expression = (f"expression DatabaseQuery =\n\t\tlet\n\t\t\tdatabase = Sql.Database(\"{sql['connectionString']}\", "
                  f"\"{sql['id']}\")\n\t\tin\n\t\t\tdatabase\n")
    parts = [part("definition.pbism", json.dumps({"version": "4.0", "settings": {}})),
             part("definition/database.tmdl", "database\n\tcompatibilityLevel: 1604\n"),
             part("definition/model.tmdl", "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"),
             part("definition/expressions.tmdl", expression)]
    parts += [part(f"definition/tables/{t}.tmdl", table_tmdl(t, schemas[t])) for t in MODEL_TABLES]
    return upsert(ws, "semanticModels", MODEL, parts)


SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
THEME = {"name": "Ghost Listing", "dataColors": ["#0B5CFF", "#5B3DF5", "#0F8A5F", "#B26A00", "#C4321F", "#6B3FD4"],
         "background": "#FFFFFF", "foreground": "#0F1B2D", "tableAccent": "#0B5CFF"}


def field(table: str, name: str) -> dict:
    kind = "Measure" if any(name in m for m in MEASURES.values()) else "Column"
    return {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def visual(name, vtype, x, y, w, h, title, values=(), category=None, table="run_outcomes", sort=None) -> tuple:
    """One PBIR visual.json. values: measures or columns of `table`; sort: field to sort descending by."""
    proj = lambda n: {"field": field(table, n), "queryRef": f"{table}.{n}", "nativeQueryRef": n}
    state = ({"Category": {"projections": [proj(category)]}, "Y": {"projections": [proj(v) for v in values]}}
             if category else {"Values": {"projections": [proj(v) for v in values]}})
    query = {"queryState": state}
    if sort:
        query["sortDefinition"] = {"sort": [{"field": field(table, sort), "direction": "Descending"}]}
    lit = lambda v: {"expr": {"Literal": {"Value": v}}}
    return name, {"$schema": f"{SCHEMA}/visualContainer/1.0.0/schema.json", "name": name,
                  "position": {"x": x, "y": y, "z": 0, "width": w, "height": h},
                  "visual": {"visualType": vtype, "query": query, "visualContainerObjects": {"title": [{"properties": {
                      "show": lit("false" if vtype == "card" else "true"), "text": lit(f"'{title}'")}}]}}}


def report(ws: str, model_id: str) -> str:
    cards = [("Exposure Found", "run_outcomes"), ("Exposure Removed", "run_outcomes"), ("Listings Checked", "run_outcomes"),
             ("Calls Placed", "run_outcomes"), ("Needs Review", "run_outcomes"), ("Open Flagged $", "claim_flags")]
    vis = [visual(f"card{i}", "card", 10 + i * 211, 10, 201, 120, m, [m], table=t) for i, (m, t) in enumerate(cards)]
    vis += [visual("byOutcome", "clusteredBarChart", 10, 145, 625, 260, "Exposure ($/month) by outcome",
                   ["Exposure Found"], "outcome", sort="Exposure Found"),
            visual("byFlag", "clusteredBarChart", 645, 145, 625, 260, "Open flagged claims ($) by reason",
                   ["Open Flagged $"], "flag_reason", table="claim_flags", sort="Open Flagged $"),
            visual("listings", "tableEx", 10, 420, 1260, 290, "Listings checked, by dollar exposure",
                   ["listing_id", "full_name", "credential", "claims_verdict", "p_wrong", "exposure_per_month",
                    "source", "outcome", "front_desk"], sort="exposure_per_month")]
    conn = (f'Data Source="powerbi://api.powerbi.com/v1.0/myorg/{WORKSPACE}";initial catalog="{MODEL}";'
            f"access mode=readonly;integrated security=ClaimsToken;semanticmodelid={model_id}")
    pbir = {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/1.0.0/schema.json",
            "version": "4.0", "datasetReference": {"byConnection": {
                "connectionString": conn, "pbiServiceModelId": None, "pbiModelVirtualServerName": "sobe_wowvirtualserver",
                "pbiModelDatabaseName": model_id, "name": "EntityDataSource", "connectionType": "pbiServiceXmlaStyleLive"}}}
    parts = [part("definition.pbir", json.dumps(pbir)),
             part("definition/version.json", json.dumps({"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})),
             part("definition/report.json", json.dumps({
                 "$schema": f"{SCHEMA}/report/1.0.0/schema.json",
                 "themeCollection": {"customTheme": {"name": "GhostTheme.json", "reportVersionAtImport": "5.55",
                                                     "type": "RegisteredResources"}},
                 "layoutOptimization": "None",
                 "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources",
                                       "items": [{"name": "GhostTheme.json", "path": "GhostTheme.json", "type": "CustomTheme"}]}]})),
             part("StaticResources/RegisteredResources/GhostTheme.json", json.dumps(THEME)),
             part("definition/pages/pages.json", json.dumps({"$schema": f"{SCHEMA}/pagesMetadata/1.0.0/schema.json",
                                                             "pageOrder": ["main"], "activePageName": "main"})),
             part("definition/pages/main/page.json", json.dumps({"$schema": f"{SCHEMA}/page/1.0.0/schema.json", "name": "main",
                                                                 "displayName": "Directory Integrity", "displayOption": "FitToPage",
                                                                 "height": 720, "width": 1280}))]
    parts += [part(f"definition/pages/main/visuals/{n}/visual.json", json.dumps(v)) for n, v in vis]
    old = find(call("GET", f"/workspaces/{ws}/reports")["value"], REPORT)
    if old:  # replace: an older publish may be in the legacy format, which can't be updated to PBIR in place
        call("DELETE", f"/workspaces/{ws}/reports/{old['id']}")
    call("POST", f"/workspaces/{ws}/reports", {"displayName": REPORT, "definition": {"format": "PBIR", "parts": parts}})
    return find(call("GET", f"/workspaces/{ws}/reports")["value"], REPORT)["id"]


if __name__ == "__main__":
    ws = workspace()
    lh = lakehouse(ws)
    print(f"workspace {ws}\nlakehouse {lh['id']}")
    schemas = {}
    stamp = time.strftime("%Y%m%d-%H%M%S")  # a fresh folder per publish: the loader caches files by path
    for csv_path in sorted(SRC.glob("*.csv")):
        data, schemas[csv_path.stem] = to_parquet(csv_path)
        name = f"{stamp}/{csv_path.stem}.parquet"
        upload(ws, lh["id"], name, data)
        # "Overwrite" keeps the old column list, so drop the table first in case columns changed
        requests.delete(f"{ONELAKE}/{ws}/{lh['id']}/Tables/{csv_path.stem}", params={"recursive": "true"},
                        headers=STORAGE)
        time.sleep(5)
        call("POST", f"/workspaces/{ws}/lakehouses/{lh['id']}/tables/{csv_path.stem}/load",
             {"relativePath": f"Files/fabric_export/{name}", "pathType": "File",
              "mode": "Overwrite", "formatOptions": {"format": "Parquet"}})
        print(f"table {csv_path.stem}: loaded")
    sql = lh["properties"]["sqlEndpointProperties"]
    try:  # make the SQL endpoint see the new tables before Direct Lake reads them
        call("POST", f"/workspaces/{ws}/sqlEndpoints/{sql['id']}/refreshMetadata", {})
    except Exception as e:
        print(f"(metadata refresh skipped: {e})")
        time.sleep(30)
    model_id = semantic_model(ws, sql, schemas)
    print(f"semantic model {model_id}")
    report_id = report(ws, model_id)
    print(f"report: https://app.fabric.microsoft.com/groups/{ws}/reports/{report_id}")
