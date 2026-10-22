from reincarnate import interfaces as I


def test_normalize_token_rules():
    assert I.normalize_token("Auth:User_Identity") == "auth:user-identity"
    assert I.normalize_token("store:  layout  config ") == "store:layout-config"
    assert I.normalize_token("bogus:thing") == ""      # unknown kind
    assert I.normalize_token("nocolon") == ""
    assert I.normalize_token("auth:") == ""
    assert I.normalize_token("auth:-") == ""
    assert I.normalize_token("auth:" + "a" * 60) == ""  # too long
    assert I.normalize_token(None) == ""
    assert I.normalize_token("auth:two:colons") == ""


def test_normalize_list_dedupes_caps_and_rejects_shapes():
    assert I.normalize_list("auth:x") == []
    assert I.normalize_list(["auth:x", "AUTH:x", "nope", 5]) == ["auth:x"]
    many = [f"data:t{i}" for i in range(10)]
    assert len(I.normalize_list(many)) == I.MAX_TOKENS_PER_SIDE


def test_canonicalize_alias_and_drop():
    v = I.Vocabulary(
        tokens={"data:peer-connection": {"definition": "d", "platform": False}},
        aliases={"data:p2p": "data:peer-connection", "config:junk": None, "ext:docker": "x:y"})
    assert v.canonicalize(["data:p2p", "data:peer-connection", "config:junk"]) == [
        "data:peer-connection"]
    assert v.canonicalize(["ext:docker"]) == ["ext:docker"]   # ext never aliased
    assert v.canonicalize(["data:other"]) == ["data:other"]


def test_vocabulary_platform_block_bool():
    v = I.Vocabulary(tokens={"http:api-server": {"definition": "the server", "platform": True},
                             "auth:session": {"definition": "a session", "platform": False}})
    assert v and not I.Vocabulary()
    assert v.platform == {"http:api-server"}
    assert "auth:\n  auth:session — a session" in v.block()


def test_check_wiring_classes():
    ifaces = {
        "a": (["data:status"], ["store:registry", "ext:docker"]),
        "b": (["store:registry"], ["data:status", "config:key", "http:server"]),
        "c": (["auth:session"], ["data:status"]),
        "d": ([], []),
        "e": (["data:other"], ["store:private", "auth:session", "data:nothing"]),
    }
    w = I.check_wiring(["a", "b", "d", "e"], ifaces, platform={"http:server"})
    assert {(x["token"], x["consumer"], x["provider"]) for x in w.wired} == {
        ("store:registry", "a", "b"), ("data:status", "b", "a")}
    assert w.external == [{"token": "ext:docker", "consumer": "a"}]
    assert w.settings == [{"token": "config:key", "consumer": "b"}]
    assert w.platform == [{"token": "http:server", "consumer": "b"}]
    assert w.owned == [{"token": "store:private", "consumer": "e"}]
    assert w.unknown == ["d"]
    unmet = {x["token"]: x for x in w.unmet}
    assert unmet["auth:session"]["candidates"] == ["c"]
    assert unmet["data:nothing"]["candidates"] == []
    assert w.as_dict()["counts"]["wired"] == 2
