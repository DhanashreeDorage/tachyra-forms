import io, json, os, tempfile
os.environ["SQLITE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["ALLOWED_ORIGINS"] = "https://tachyradiagnostics.com"
os.environ["ADMIN_TOKEN"] = "test-token"
import app as A
KEEP=[False]

def call(method, path, body=None, origin="https://tachyradiagnostics.com", headers=None):
    data = json.dumps(body).encode() if body is not None else b""
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(data)),
           "wsgi.input": io.BytesIO(data), "REMOTE_ADDR": "1.2.3.4", "HTTP_ORIGIN": origin}
    env.update(headers or {})
    out = {}
    def start(status, hdrs): out["status"] = status; out["headers"] = dict(hdrs)
    resp = b"".join(A.application(env, start))
    if not KEEP[0]: A._hits.clear()
    return out["status"], out["headers"], resp

good = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com", "credential": "MD",
        "specialty": "Family medicine", "license_state": "FL", "license_number": "ME12345",
        "ack_test_data": True, "consent_recognition": False, "elapsed_ms": 9000, "company_website": ""}
s, h, b = call("POST", "/submit/founding", good); assert s.startswith("200") and json.loads(b)["ok"], (s, b)
assert h.get("Access-Control-Allow-Origin") == "https://tachyradiagnostics.com"
s, h, b = call("OPTIONS", "/submit/founding"); assert s.startswith("204")
bad = dict(good, email="not-an-email"); s, h, b = call("POST", "/submit/founding", bad); assert s.startswith("422"), s
bad = dict(good, ack_test_data=False); s, h, b = call("POST", "/submit/founding", bad); assert s.startswith("422")
bad = dict(good, credential="RN"); s, h, b = call("POST", "/submit/founding", bad); assert s.startswith("422")
s, h, b = call("POST", "/submit/founding", good, origin="https://evil.example"); assert s.startswith("403")
spam = dict(good, company_website="http://spam"); s, h, b = call("POST", "/submit/founding", spam); assert s.startswith("200")
fast = dict(good, elapsed_ms=200); s, h, b = call("POST", "/submit/founding", fast); assert s.startswith("200")
s, h, b = call("POST", "/submit/nope", good); assert s.startswith("404")
inv = {"first_name":"A","last_name":"B","email":"a@b.co","state":"FL","accredited":"Yes","ack_not_offer":True,"elapsed_ms":5000}
s,h,b = call("POST","/submit/investor",inv); assert s.startswith("200"), b
d = {"first_name":"A","last_name":"B","credential":"DO","npi":"123456789","specialty":"IM","email":"a@b.co","practice":"P","street":"1 Main","city":"Boca","state":"FL","zip":"33431","practice_phone":"561-555-0100","consent_listing":True,"elapsed_ms":5000}
s,h,b = call("POST","/submit/directory",d); assert s.startswith("422") and b"NPI" in b
d["npi"]="1234567890"; s,h,b = call("POST","/submit/directory",d); assert s.startswith("200"), b
s,h,b = call("POST","/submit/directory_notify",{"email":"p@q.com","zip":"33446","elapsed_ms":4000}); assert s.startswith("200")
p = {"full_name":"Dr X","email":"x@y.com","credential":"MD","specialty":"FM","practice":"P","city_state":"Delray Beach, FL","active_patients":"301-750","ack_no_phi":True,"elapsed_ms":4000}
s,h,b = call("POST","/submit/pilot",p); assert s.startswith("200"), b
s, h, b = call("GET", "/export/founding.csv", headers={"HTTP_X_ADMIN_TOKEN": "wrong"}); assert s.startswith("401")
s, h, b = call("GET", "/export/founding.csv", headers={"HTTP_X_ADMIN_TOKEN": "test-token"}); assert s.startswith("200")
rows = b.decode().strip().splitlines(); assert len(rows) == 2 and "Jane" in rows[1], rows   # spam/fast not saved
KEEP[0]=True
for i in range(8): call("POST","/submit/directory_notify",{"email":"p@q.com","elapsed_ms":4000})
s,h,b = call("POST","/submit/directory_notify",{"email":"p@q.com","elapsed_ms":4000}); assert s.startswith("429"), s
big = dict(good, notes="x"*30000); s,h,b = call("POST","/submit/founding",big); assert s.startswith("413")
print("ALL TESTS PASSED")
