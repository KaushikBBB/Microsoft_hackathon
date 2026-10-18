"""Generate synthetic data for the Ghost Listing Verifier prototype.

One insurer, one plan, Chicago metro. Writes CSVs to data/seed/:
  therapists.csv          50 therapists
  therapist_listings.csv  ~60 listings (some therapists have two offices)
  patients.csv            450 plan members
  claims.csv              18 months of paid claims
  directory_views.csv     90 days of members viewing listings
  complaints.csv          members reporting a listing that let them down

About 30% of listings are planted ghosts (true_status != 'active_open').
Claims, views and complaints are all driven by true_status, so the signals
the triage model learns from are consistent with the hidden answer key.
All phone numbers use the 555-01xx block reserved for fiction, so a test
dialer can never reach a real office.

Usage: python3 data/generate.py
"""
import csv
import hashlib
import math
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

SEED = 42
N_THERAPISTS = 50
N_PATIENTS = 450
TODAY = date(2026, 10, 15)
OUT = Path(__file__).parent / "seed"
CLAIMS_FROM = TODAY - timedelta(days=548)   # 18 months of claims history
VIEWS_FROM = TODAY - timedelta(days=90)
N_VIEWS = 3000

rng = random.Random(SEED)

# zip -> (city, lat, lon, area_code, member_weight, streets)
# Centroids are approximate. member_weight sets where patients live.
ZIPS = {
    "60601": ("Chicago", 41.8858, -87.6181, "312", 3, ["N Michigan Ave", "E Lake St", "N Wabash Ave"]),
    "60607": ("Chicago", 41.8721, -87.6578, "312", 4, ["W Madison St", "S Halsted St", "W Jackson Blvd"]),
    "60608": ("Chicago", 41.8515, -87.6694, "773", 6, ["W Cermak Rd", "S Ashland Ave", "W 18th St"]),
    "60610": ("Chicago", 41.9033, -87.6336, "312", 4, ["N Clark St", "N LaSalle Dr", "W Division St"]),
    "60611": ("Chicago", 41.8949, -87.6202, "312", 3, ["E Ohio St", "N Rush St", "E Chicago Ave"]),
    "60614": ("Chicago", 41.9227, -87.6533, "773", 6, ["N Lincoln Ave", "W Fullerton Ave", "N Halsted St"]),
    "60615": ("Chicago", 41.8021, -87.6006, "773", 4, ["S Lake Park Ave", "E 53rd St", "S Cottage Grove Ave"]),
    "60618": ("Chicago", 41.9464, -87.7042, "773", 7, ["W Irving Park Rd", "N Kedzie Ave", "W Belmont Ave"]),
    "60622": ("Chicago", 41.9024, -87.6767, "773", 5, ["W Chicago Ave", "N Damen Ave", "N Milwaukee Ave"]),
    "60625": ("Chicago", 41.9703, -87.7043, "773", 6, ["W Lawrence Ave", "N Kimball Ave", "W Foster Ave"]),
    "60626": ("Chicago", 42.0092, -87.6688, "773", 5, ["N Sheridan Rd", "W Howard St", "N Clark St"]),
    "60637": ("Chicago", 41.7816, -87.6037, "773", 5, ["E 63rd St", "S Ellis Ave", "S Woodlawn Ave"]),
    "60640": ("Chicago", 41.9719, -87.6624, "773", 6, ["N Broadway", "W Lawrence Ave", "N Clark St"]),
    "60647": ("Chicago", 41.9210, -87.7016, "773", 7, ["N Milwaukee Ave", "W Armitage Ave", "N California Ave"]),
    "60657": ("Chicago", 41.9399, -87.6528, "773", 6, ["N Broadway", "W Belmont Ave", "N Southport Ave"]),
    "60201": ("Evanston", 42.0546, -87.6943, "847", 4, ["Church St", "Davis St", "Chicago Ave"]),
    "60302": ("Oak Park", 41.8944, -87.7895, "708", 4, ["Lake St", "Oak Park Ave", "Madison St"]),
    "60540": ("Naperville", 41.7664, -88.1411, "630", 3, ["W Jefferson Ave", "S Washington St", "E Ogden Ave"]),
    "60115": ("DeKalb", 41.9299, -88.7504, "815", 2, ["E Lincoln Hwy", "N 1st St", "Sycamore Rd"]),
}

FIRST = ["Maria", "James", "Aisha", "Daniel", "Priya", "Michael", "Elena", "Robert", "Grace", "Carlos",
         "Hannah", "David", "Fatima", "Kevin", "Olivia", "Marcus", "Sofia", "Brian", "Leah", "Anthony",
         "Mei", "Samuel", "Rachel", "Jamal", "Natalia", "Thomas", "Rosa", "Andrew", "Nadia", "Victor",
         "Laura", "Ethan", "Ines", "Patrick", "Yuki", "Gregory", "Amara", "Jonah", "Teresa", "Omar"]
LAST = ["Alvarez", "Bennett", "Chen", "Dabrowski", "Edwards", "Fischer", "Garcia", "Hassan", "Iyer",
        "Johnson", "Kowalski", "Lopez", "Morales", "Nguyen", "Okafor", "Patel", "Quinn", "Rivera",
        "Schneider", "Thompson", "Underwood", "Vasquez", "Washington", "Xu", "Yamamoto", "Zielinski",
        "Brooks", "Castillo", "Donovan", "Feldman", "Gutierrez", "Holloway", "Kaminski", "Levine",
        "Mendoza", "Novak", "Ortiz", "Reyes", "Sullivan", "Walsh"]

# credential -> (weight, Illinois-style license number prefix)
CREDENTIALS = {
    "LCSW": (14, "149"), "LPC": (12, "180"), "LMFT": (8, "168"),
    "PsyD": (6, "071"), "PhD": (6, "071"), "MD": (4, "036"),
}


# procedure code -> typical paid amount
PAID = {
    "90791": 175,   # psychiatric diagnostic evaluation (new-patient intake)
    "90792": 210,   # same, with medical services (MD intake)
    "90834": 105,   # psychotherapy, 45 min
    "90837": 135,   # psychotherapy, 60 min
    "90847": 115,   # family psychotherapy
    "99214": 120,   # office visit, medication management (MD)
}

# true_status -> (chance a member who called files a complaint, category weights)
COMPLAINTS = {
    "active_open":   (0.01, {"couldnt_book": 1}),
    "full_caseload": (0.15, {"couldnt_book": 1}),
    "moved":         (0.20, {"wrong_number": 1, "couldnt_book": 1}),
    "retired":       (0.25, {"wrong_number": 3, "couldnt_book": 2}),
    "wrong_phone":   (0.30, {"wrong_number": 1}),
}


def npi() -> str:
    """Random NPI with a valid Luhn check digit (computed with the 80840 prefix)."""
    body = "1" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    digits = [int(d) for d in "80840" + body]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:  # doubled positions, counted from the right of the payload
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return body + str((10 - total % 10) % 10)


def rand_date(start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def jitter(lat: float, lon: float, km: float = 1.2) -> tuple:
    """Move a point up to ~km away so offices aren't all on the zip centroid."""
    return (round(lat + rng.uniform(-km, km) / 111, 6),
            round(lon + rng.uniform(-km, km) / 83, 6))


used_phones = set()


def fake_phone(area: str) -> str:
    while True:
        p = f"({area}) 555-01{rng.randint(0, 99):02d}"
        if p not in used_phones:
            used_phones.add(p)
            return p


def gen_therapists():
    names = set()
    rows = []
    creds = list(CREDENTIALS)
    weights = [CREDENTIALS[c][0] for c in creds]
    for _ in range(N_THERAPISTS):
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name not in names:
                names.add(name)
                break
        cred = rng.choices(creds, weights)[0]
        langs = ["English"]
        r = rng.random()
        if r < 0.22:
            langs.append("Spanish")
        elif r < 0.27:
            langs.append("Polish")
        elif r < 0.30:
            langs.append("Mandarin")
        elif r < 0.32:
            langs.append("Hindi")
        rows.append({
            "therapist_id": npi(),
            "full_name": name + (", MD" if cred == "MD" else ""),
            "credential": cred,
            "license_number": f"{CREDENTIALS[cred][1]}.{rng.randint(10000, 99999):06d}",
            "license_state": "IL",
            "license_status": "active",
            "license_expires_on": rand_date(date(2026, 11, 30), date(2028, 11, 30)),
            "sanctioned": False,
            "languages": langs,
            "offers_telehealth": rng.random() < 0.6,
            "contract_start": rand_date(date(2014, 1, 1), date(2026, 6, 30)),
            "contract_end": None,
        })
    return rows


def gen_listings(therapists):
    """Plant ghosts. Therapist-level problems (retired, full caseload) apply
    to every office; office-level problems (moved, wrong phone) to one."""
    ids = [t["therapist_id"] for t in therapists]
    rng.shuffle(ids)
    retired = set(ids[:3])
    full = set(ids[3:9])
    two_offices = set(rng.sample(ids, 12))
    by_id = {t["therapist_id"]: t for t in therapists}

    # Hard-fail cases the triage rules must catch without a call
    t = by_id[ids[0]]                       # retired, let the license lapse
    t["license_status"] = "lapsed"
    t["license_expires_on"] = date(2026, 5, 31)
    t = by_id[ids[9]]                       # sanctioned, still seeing patients
    t["sanctioned"] = True
    t = by_id[ids[10]]                      # left the network, listing never removed
    t["contract_end"] = date(2026, 7, 31)

    zips = list(ZIPS)
    rows, n = [], 0
    for tid in sorted(ids):
        offices = rng.sample(zips, 2 if tid in two_offices else 1)
        for z in offices:
            n += 1
            city, lat, lon, area, _, streets = ZIPS[z]
            if tid in retired:
                status = "retired"
            elif tid in full:
                status = "full_caseload"
            else:
                status = "active_open"
            olat, olon = jitter(lat, lon)
            rows.append({
                "listing_id": f"L{n:04d}",
                "therapist_id": tid,
                "address": f"{rng.randint(100, 4999)} {rng.choice(streets)}"
                           + (f", Suite {rng.randint(1, 9)}0{rng.randint(1, 9)}" if rng.random() < 0.5 else ""),
                "city": city, "zip": z, "lat": olat, "lon": olon,
                "phone": fake_phone(area),
                "true_status": status,
            })

    # Office-level ghosts on otherwise healthy listings
    healthy = [r for r in rows if r["true_status"] == "active_open"]
    for r in rng.sample(healthy, 8):
        r["true_status"] = "moved" if len([x for x in rows if x["true_status"] == "moved"]) < 4 else "wrong_phone"

    for r in rows:
        ghost = r["true_status"] != "active_open"
        # The directory still says "accepting" for most ghosts: that's the problem
        r["accepting_new"] = rng.random() < (0.85 if ghost else 0.8)
        # Ghosts tend to have gone longer without verification
        r["last_verified_on"] = TODAY - timedelta(days=rng.randint(60, 240) if ghost else rng.randint(5, 150))
        r["last_verified_how"] = rng.choices(["attestation", "call", "claims"], [60, 25, 15])[0]
        r["is_suppressed"] = False
    return rows


def gen_patients():
    zips = list(ZIPS)
    weights = [ZIPS[z][4] for z in zips]
    rows = []
    for i in range(N_PATIENTS):
        z = rng.choices(zips, weights)[0]
        _, lat, lon, *_ = ZIPS[z]
        enrolled_on = rand_date(date(2018, 1, 1), date(2026, 9, 1))
        until = None
        if rng.random() < 0.08 and enrolled_on < TODAY - timedelta(days=120):
            until = rand_date(enrolled_on + timedelta(days=90), TODAY - timedelta(days=1))
        rows.append({
            "patient_id": hashlib.sha256(f"member-{SEED}-{i}".encode()).hexdigest()[:16],
            "age_band": rng.choices(["18-25", "26-40", "41-64", "65+"], [20, 38, 32, 10])[0],
            "home_zip": z, "home_lat": lat, "home_lon": lon,
            "enrolled_on": enrolled_on, "enrolled_until": until,
        })
    return rows


def km(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((float(lat1) - float(lat2)) * 111, (float(lon1) - float(lon2)) * 83)


def enrolled(p, d: date) -> bool:
    return p["enrolled_on"] <= d and (p["enrolled_until"] is None or d <= p["enrolled_until"])


def gen_claims(therapists, listings, patients):
    """Claims show where care really happens. Each listing gets episodes of
    care (an intake, then a visit every week or two) shaped by true_status:
      retired        all visits stop on a retirement date
      full_caseload  ongoing visits continue, but no new-patient intakes
      moved          visits continue at an office not in the directory (listing_id null)
      wrong_phone    fewer new patients get through
    A therapist whose contract ended has no in-network claims after that date."""
    by_id = {t["therapist_id"]: t for t in therapists}
    retired_on, full_since = {}, {}   # therapist-level, shared by both offices
    rows = []
    for l in listings:
        t = by_id[l["therapist_id"]]
        tid, status = t["therapist_id"], l["true_status"]
        visits_until = t["contract_end"] or TODAY
        intakes_until = visits_until
        moved_on = None
        rate = rng.uniform(0.3, 0.7) / 30   # new patients per day
        if status == "retired":
            visits_until = intakes_until = retired_on.setdefault(
                tid, TODAY - timedelta(days=rng.randint(120, 300)))
        elif status == "full_caseload":
            intakes_until = full_since.setdefault(tid, TODAY - timedelta(days=rng.randint(120, 270)))
        elif status == "moved":
            moved_on = TODAY - timedelta(days=rng.randint(90, 240))
        elif status == "wrong_phone":
            rate *= 0.5

        # Start before the window so the earliest months already have ongoing patients
        day = CLAIMS_FROM - timedelta(days=120)
        while True:
            day += timedelta(days=max(1, round(rng.expovariate(rate))))
            if day > intakes_until:
                break
            pool = [p for p in patients if enrolled(p, day)]
            p = rng.choices(pool, [1 / (km(l["lat"], l["lon"], q["home_lat"], q["home_lon"]) + 2) ** 2
                                   for q in pool])[0]
            visit = day
            for k in range(rng.randint(4, 40 if status == "full_caseload" else 24) + 1):
                if visit > visits_until or not enrolled(p, visit):
                    break
                code = session_code(t, intake=(k == 0))
                received = visit + timedelta(days=rng.randint(7, 45))
                if visit >= CLAIMS_FROM and received <= TODAY:   # not yet received = not in the system
                    rows.append({
                        "therapist_id": tid,
                        "listing_id": None if moved_on and visit >= moved_on else l["listing_id"],
                        "patient_id": p["patient_id"],
                        "service_date": visit, "received_date": received,
                        "procedure_code": code,
                        "paid_amount": f"{PAID[code] * rng.uniform(0.92, 1.08):.2f}",
                    })
                visit += timedelta(days=rng.choice([7, 7, 7, 14]) + rng.randint(-1, 1))

    rows.sort(key=lambda r: (r["received_date"], r["service_date"], r["therapist_id"]))
    for i, r in enumerate(rows, 1):
        r["claim_id"] = f"C{i:06d}"
    return rows


def session_code(t, intake: bool) -> str:
    if t["credential"] == "MD":
        return "90792" if intake else "99214"
    if intake:
        return "90791"
    if t["credential"] == "LMFT" and rng.random() < 0.3:
        return "90847"
    return "90837" if rng.random() < 0.6 else "90834"


def gen_views(listings, patients):
    """Members search the directory near home; listings that say they accept
    new patients get more attention. Ghosts are viewed like any other listing."""
    rows = []
    span = (TODAY - VIEWS_FROM).days
    for _ in range(N_VIEWS):
        d = VIEWS_FROM + timedelta(days=rng.randrange(span))
        p = rng.choice([q for q in patients if enrolled(q, d)])
        l = rng.choices(listings, [(1.5 if x["accepting_new"] else 1)
                                   / (km(x["lat"], x["lon"], p["home_lat"], p["home_lon"]) + 3) ** 2
                                   for x in listings])[0]
        rows.append({
            "listing_id": l["listing_id"], "patient_id": p["patient_id"],
            "viewed_at": datetime.combine(d, time(rng.randint(7, 22), rng.randrange(60), rng.randrange(60))),
            "clicked_phone": rng.random() < 0.35,
        })
    rows.sort(key=lambda r: r["viewed_at"])
    for i, r in enumerate(rows, 1):
        r["view_id"] = f"V{i:06d}"
    return rows


def gen_complaints(therapists, listings, views):
    """Some members who called a ghost listing complain a few days later."""
    by_listing = {l["listing_id"]: l for l in listings}
    ended = {t["therapist_id"]: t["contract_end"] for t in therapists if t["contract_end"]}
    rows = []
    for v in views:
        if not v["clicked_phone"]:
            continue
        l = by_listing[v["listing_id"]]
        d = v["viewed_at"].date()
        if l["therapist_id"] in ended and d > ended[l["therapist_id"]]:
            odds, cats = 0.20, {"not_in_network": 1}
        else:
            odds, cats = COMPLAINTS[l["true_status"]]
        received = d + timedelta(days=rng.randint(1, 14))
        if rng.random() < odds and received <= TODAY:
            rows.append({
                "listing_id": l["listing_id"], "patient_id": v["patient_id"], "received_on": received,
                "category": rng.choices(list(cats), list(cats.values()))[0],
            })
    rows.sort(key=lambda r: r["received_on"])
    for i, r in enumerate(rows, 1):
        r["complaint_id"] = f"Q{i:05d}"
    return rows


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "{" + ",".join(v) + "}"
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    return str(v)


def write(name, rows, cols):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{name}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([fmt(r[c]) for c in cols])


if __name__ == "__main__":
    therapists = gen_therapists()
    listings = gen_listings(therapists)
    patients = gen_patients()
    claims = gen_claims(therapists, listings, patients)
    views = gen_views(listings, patients)
    complaints = gen_complaints(therapists, listings, views)

    write("therapists", therapists, list(therapists[0]))
    write("therapist_listings", listings,
          ["listing_id", "therapist_id", "address", "city", "zip", "lat", "lon", "phone",
           "accepting_new", "last_verified_on", "last_verified_how", "is_suppressed", "true_status"])
    write("patients", patients, list(patients[0]))
    write("claims", claims, ["claim_id", "therapist_id", "listing_id", "patient_id", "service_date",
                             "received_date", "procedure_code", "paid_amount"])
    write("directory_views", views, ["view_id", "listing_id", "patient_id", "viewed_at", "clicked_phone"])
    write("complaints", complaints, ["complaint_id", "listing_id", "patient_id", "received_on", "category"])

    from collections import Counter
    print(f"therapists: {len(therapists)}  listings: {len(listings)}  patients: {len(patients)}")
    print(f"claims: {len(claims)}  directory_views: {len(views)}  complaints: {len(complaints)}")
    print("complaint categories:", dict(Counter(r["category"] for r in complaints)))
    print("listing true_status:", dict(Counter(r["true_status"] for r in listings)))
    ghosts = sum(r["true_status"] != "active_open" for r in listings)
    print(f"ghost share: {ghosts / len(listings):.0%}")
