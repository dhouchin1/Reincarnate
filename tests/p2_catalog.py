"""A small catalog shared by the P2 tests: three donors, wiring between them, one denied
license (AGPL-3.0), one commodity part."""

from reincarnate.models import Profile, Project


def _cap(name, what, portability="high", provides=(), requires=(), novelty="distinctive",
         security=""):
    return {"name": name, "what": what, "portability": portability, "files": ["secret/x.py"],
            "provides": list(provides), "requires": list(requires), "novelty": novelty,
            "security_notes": security}


def _dossier(license_, caps):
    return {"summary": "A donor.", "dependencies": {"license": license_}, "capabilities": caps}


def seed(store) -> dict[str, int]:
    """-> {capability name: id}."""
    store.add_project(Project(slug="homelab", path="/x/homelab", goal="servers"))
    store.save_profile(Profile(name="dan", stack=["python"],
                               license_policy={"deny": ["AGPL-3.0"], "warn": ["unknown"]}))
    store.upsert_dossier("acme", "authkit", "https://github.com/acme/authkit", "a1", _dossier(
        "MIT",
        [_cap("Session login", "password login with cookie sessions", "low",
              provides=["auth:session"], requires=["store:users"],
              security="hashes passwords"),
         _cap("Retry helper", "retry flaky calls with backoff", provides=["data:retry-policy"],
              novelty="commodity")]))
    store.upsert_dossier("acme", "boards", "https://github.com/acme/boards", "b1", _dossier(
        "MIT",
        [_cap("Team dashboard", "team dashboard of drag and drop tiles",
              provides=["ui:dashboard"],
              requires=["auth:session", "store:board-layout", "config:theme"]),
         _cap("Layout grid", "drag and drop grid layout for tiles",
              provides=["store:board-layout"]),
         _cap("Activity feed", "team activity feed", "medium", provides=["event:activity"])]))
    store.upsert_dossier("evil", "notify", "https://github.com/evil/notify", "c1", _dossier(
        "AGPL-3.0",
        [_cap("Email notifier", "send email alerts", "medium", provides=["notify:email"],
              requires=["ext:smtp", "event:activity"]),
         _cap("Digest scheduler", "schedule a daily digest", "low",
              provides=["schedule:daily"])]))
    return {c.name: c.id for c in store.list_capabilities()}
