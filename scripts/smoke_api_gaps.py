import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import django, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.test.utils import setup_test_environment
setup_test_environment()
from django.test import Client
from music.models import Track, TrackComment
from playlists.models import Favorite, PlayHistory, PlaybackQueue

t = Track.objects.published().first()
print("seed track:", t.pk, t.title)

c = Client()
print("--- anonymous ---")
r = c.get("/api/recommendations/")
print("GET /api/recommendations/", r.status_code, "personalised=", r.json().get("personalised"),
      "tracks=", len(r.json().get("tracks", [])))
r = c.get(f"/api/tracks/{t.pk}/similar/?limit=5")
print("GET similar", r.status_code, [x["title"] for x in r.json()][:5])
r = c.get(f"/api/tracks/{t.pk}/comments/")
print("GET comments", r.status_code, len(r.json() if isinstance(r.json(), list) else r.json().get("results", [])))
r = c.get("/api/queue/")
print("GET /api/queue/ anon ->", r.status_code)
r = c.get("/api/comments/1/")
print("GET comment anon ->", r.status_code)

print("--- authed ---")
assert c.login(username="listener@auralis.test", password="Listen!2345"), "login failed"
me = __import__("django.contrib.auth", fromlist=["get_user_model"]).get_user_model().objects.get(email="listener@auralis.test")

r = c.post(f"/api/tracks/{t.pk}/comments/", data=json.dumps({"body": "Great track!"}), content_type="application/json")
print("POST comment", r.status_code, r.json().get("author"), repr(r.json().get("body")))
cid = r.json()["id"]

r = c.post(f"/api/tracks/{t.pk}/comments/", data=json.dumps({"body": "   "}), content_type="application/json")
print("POST blank comment ->", r.status_code, r.json())

r = c.patch(f"/api/comments/{cid}/", data=json.dumps({"status": "hidden"}), content_type="application/json")
print("PATCH status as author ->", r.status_code, r.json())

r = c.patch(f"/api/comments/{cid}/", data=json.dumps({"body": "Edited body"}), content_type="application/json")
print("PATCH body as author ->", r.status_code, repr(r.json().get("body")))

r = c.get(f"/api/tracks/{t.pk}/comments/")
print("GET comments after edit", r.status_code, len(r.json().get("results", r.json()) if isinstance(r.json(), dict) else r.json()))

print("--- queue ---")
r = c.get("/api/queue/")
print("GET /api/queue/ (empty) ->", r.status_code, r.json())

ids = [x.pk for x in Track.objects.published()[:3]]
draft = Track.objects.filter(status=Track.Status.DRAFT).first()
payload = {"track_ids": ids + ([draft.pk] if draft else []), "current_position": 1, "shuffle": True, "repeat": "all"}
r = c.put("/api/queue/", data=json.dumps(payload), content_type="application/json")
print("PUT /api/queue/ ->", r.status_code, "items=", len(r.json().get("items", [])), "dropped=", r.json().get("dropped_track_ids"))

r = c.get("/api/queue/")
d = r.json()
print("GET /api/queue/ ->", r.status_code, "items=", len(d["items"]), "pos=", d["current_position"], "shuffle=", d["shuffle"], "repeat=", d["repeat"])

r = c.put("/api/queue/", data=json.dumps({**payload, "current_position": 99}), content_type="application/json")
print("PUT bad position ->", r.status_code, r.json())

r = c.put("/api/queue/", data=json.dumps({"track_ids": [ids[0], ids[0]]}), content_type="application/json")
print("PUT duplicates ->", r.status_code, r.json())

r = c.delete("/api/queue/")
print("DELETE /api/queue/ ->", r.status_code, "exists=", PlaybackQueue.objects.filter(user=me).exists())

print("--- personalised ---")
r = c.get("/api/recommendations/?limit=6")
print("GET /api/recommendations/ ->", r.status_code, "personalised=", r.json()["personalised"],
      "affinity=", r.json()["genre_affinity"][:2], "tracks=", len(r.json()["tracks"]))
