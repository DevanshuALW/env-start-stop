# Slack slash command + nightly clock.
# STOP: pause parent → pause apps → delete HPA → scale 0 → cron off → RDS stop
# START: RDS up → unpause apps → cron on → unpause parent → scale leftover 0 to HPA min
# Client-specific names live in config.json (Terraform writes that file).

import base64, hashlib, hmac, json, os, ssl, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
import boto3
from botocore.exceptions import ClientError
from botocore.signers import RequestSigner


def _slack_cred(plain_key, arn_key):
    arn = os.environ.get(arn_key)
    if arn:
        return boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    return os.environ[plain_key]


def _load_config():
    path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CFG = _load_config()
TARGETS = CFG["targets"]
DEFAULTS = CFG.get("defaults") or {}
COMMANDS = tuple(CFG.get("slack_commands") or ["/env-start-stop"])
ARGOCD = os.environ.get("ARGOCD_NAMESPACE", CFG.get("argocd_namespace") or "argocd")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
RDS_WAIT, RDS_POLL, RDS_RETRIES = 780, 20, 3
_slack = {}


def slack_signing_secret():
    if "sig" not in _slack:
        _slack["sig"] = _slack_cred("SLACK_SIGNING_SECRET", "SLACK_SIGNING_SECRET_ARN")
    return _slack["sig"]


def slack_bot_token():
    if "tok" not in _slack:
        _slack["tok"] = _slack_cred("SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN_ARN")
    return _slack["tok"]


def _t(v):
    return {"type": "plain_text", "text": v}


REGIONS = [{"text": _t(r["label"]), "value": r["key"]} for r in CFG.get("regions") or []]
ACTIONS = [{"text": _t("Stop (scale down + stop RDS)"), "value": "STOP"}, {"text": _t("Start (start RDS + bring apps up)"), "value": "START"}]
lambda_client, sts, _k8s, _aws = boto3.client("lambda"), boto3.client("sts"), {}, {}


def log(**kw):
    print(json.dumps(kw, default=str))


def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def aws(svc, region):
    _aws.setdefault((svc, region), boto3.client(svc, region_name=region))
    return _aws[(svc, region)]


def header(event, name):
    return next((v for k, v in (event.get("headers") or {}).items() if k.lower() == name.lower()), None)


def body(event):
    raw = event.get("body") or ""
    return base64.b64decode(raw).decode() if event.get("isBase64Encoded") else raw


def verify_slack(event):
    ts, sig = header(event, "X-Slack-Request-Timestamp"), header(event, "X-Slack-Signature")
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    calc = "v0=" + hmac.new(slack_signing_secret().encode(), f"v0:{ts}:{body(event)}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, sig)


def slack(method, payload):
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {slack_bot_token()}", "Content-Type": "application/json; charset=utf-8"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def post(text):
    if not SLACK_CHANNEL_ID:
        return
    out = slack("chat.postMessage", {"channel": SLACK_CHANNEL_ID, "text": text})
    if not out.get("ok"):
        log(event="slack_post_failed", error=out.get("error"))


def keys_for(rk):
    return [k for k, item in TARGETS.items() if item.get("region_key") == rk]


def parents_of(targets):
    found = {}
    for item in targets:
        name = item.get("parent")
        if not name:
            continue
        found[(item["cluster"], name)] = (item, name)
    return list(found.values())


def opt(key):
    return {"text": _t(TARGETS[key]["display"]), "value": key}


def pick(options, value):
    return next((o for o in options if o["value"] == value), None)


def state_val(state, bid, aid, field="selected_option"):
    try:
        return (((state.get("values") or {}).get(bid) or {}).get(aid) or {}).get(field)
    except (TypeError, AttributeError):
        return None


def build_modal(channel_id="", region_key=None, action=None, reason=""):
    action_el = {"type": "static_select", "action_id": "action", "options": ACTIONS}
    if pick(ACTIONS, action):
        action_el["initial_option"] = pick(ACTIONS, action)
    region_el = {
        "type": "static_select", "action_id": "region",
        "placeholder": _t(CFG.get("region_placeholder") or "Pick a region"),
        "options": REGIONS,
    }
    if pick(REGIONS, region_key):
        region_el["initial_option"] = pick(REGIONS, region_key)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": CFG.get("intro_text") or "Pick a *region*."}},
        {"type": "input", "block_id": "action_block", "label": _t("Action"), "element": action_el},
        {"type": "input", "block_id": "region_block", "dispatch_action": True, "label": _t("Region"), "element": region_el},
    ]
    rks = keys_for(region_key) if region_key else []
    if rks:
        initial = [opt(k) for k in DEFAULTS.get(region_key, []) if k in TARGETS]
        element = {
            "type": "multi_static_select", "action_id": "namespace", "placeholder": _t("Select namespaces"),
            "options": [opt(k) for k in rks],
        }
        if initial:
            element["initial_options"] = initial
        blocks.append({"type": "input", "block_id": f"ns_{region_key}", "label": _t("Namespaces"), "element": element})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_Select a region to load namespaces._"}})
    reason_el = {"type": "plain_text_input", "action_id": "reason", "placeholder": _t("Optional")}
    if reason:
        reason_el["initial_value"] = reason
    blocks.append({"type": "input", "optional": True, "block_id": "reason_block", "label": _t("Reason"), "element": reason_el})
    return {
        "type": "modal", "callback_id": "start_stop_request",
        "private_metadata": json.dumps({"channel_id": channel_id or "", "region_key": region_key or ""}),
        "title": _t(CFG.get("modal_title") or "Env start/stop"), "submit": _t("Run"), "close": _t("Cancel"), "blocks": blocks,
    }


def handle_region(payload):
    view = payload.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    state = view.get("state") or {}
    rk = ((payload.get("actions") or [{}])[0].get("selected_option") or {}).get("value") or ""
    slack("views.update", {
        "view_id": view.get("id"), "hash": view.get("hash"),
        "view": build_modal(meta.get("channel_id", ""), rk, (state_val(state, "action_block", "action") or {}).get("value"), state_val(state, "reason_block", "reason", field="value") or ""),
    })
    return resp(200, {})


def eks_token(cluster, region):
    session = boto3.session.Session()
    signer = RequestSigner(sts.meta.service_model.service_id, region, "sts", "v4", session.get_credentials(), session.events)
    signed = signer.generate_presigned_url({
        "method": "GET", "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {}, "headers": {"x-k8s-aws-id": cluster}, "context": {},
    }, region_name=region, expires_in=60, operation_name="")
    return "k8s-aws-v1." + base64.urlsafe_b64encode(signed.encode()).decode().rstrip("=")


def k8s_session(cluster, region):
    key = f"{region}:{cluster}"
    cached = _k8s.get(key)
    if cached and time.time() - cached["at"] < 50:
        return cached
    c = aws("eks", region).describe_cluster(name=cluster)["cluster"]
    ctx = ssl.create_default_context(cadata=base64.b64decode(c["certificateAuthority"]["data"]).decode())
    _k8s[key] = {"endpoint": c["endpoint"], "ssl": ctx, "token": eks_token(cluster, region), "at": time.time()}
    return _k8s[key]


def k8s(item, method, path, body=None, ctype="application/merge-patch+json"):
    s = k8s_session(item["cluster"], item["region"])
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + s["token"], "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(s["endpoint"] + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=s["ssl"], timeout=30) as r:
            raw = r.read()
            return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"k8s {method} {path} -> {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e


def names(item, path):
    return [i["metadata"]["name"] for i in (k8s(item, "GET", path) or {}).get("items") or []]


def app_path(name):
    return f"/apis/argoproj.io/v1alpha1/namespaces/{ARGOCD}/applications/{name}"


def pause(item, name):
    app = k8s(item, "GET", app_path(name))
    if not app or not ((app.get("spec") or {}).get("syncPolicy") or {}).get("automated"):
        return
    k8s(item, "PATCH", app_path(name), [{"op": "remove", "path": "/spec/syncPolicy/automated"}], "application/json-patch+json")


def unpause(item, name):
    if k8s(item, "GET", app_path(name)) is None:
        return
    k8s(item, "PATCH", app_path(name), {"spec": {"syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}})


def delete_hpas(item):
    ns = item["label"]
    for name in names(item, f"/apis/autoscaling/v1/namespaces/{ns}/horizontalpodautoscalers"):
        k8s(item, "DELETE", f"/apis/autoscaling/v1/namespaces/{ns}/horizontalpodautoscalers/{name}")


def scale(item, replicas):
    ns = item["label"]
    for kind in ("deployments", "statefulsets"):
        for name in names(item, f"/apis/apps/v1/namespaces/{ns}/{kind}"):
            k8s(item, "PATCH", f"/apis/apps/v1/namespaces/{ns}/{kind}/{name}", {"spec": {"replicas": replicas}})


def cron(item, suspended):
    ns = item["label"]
    for name in names(item, f"/apis/batch/v1/namespaces/{ns}/cronjobs"):
        k8s(item, "PATCH", f"/apis/batch/v1/namespaces/{ns}/cronjobs/{name}", {"spec": {"suspend": suspended}})


def kick_from_zero(item):
    ns = item["label"]
    mins, path = {}, f"/apis/autoscaling/v1/namespaces/{ns}/horizontalpodautoscalers"
    for _ in range(6):
        for hpa in (k8s(item, "GET", path) or {}).get("items") or []:
            spec = hpa.get("spec") or {}
            target = (spec.get("scaleTargetRef") or {}).get("name")
            if target:
                mins[target] = spec.get("minReplicas") or 1
        if mins:
            break
        time.sleep(5)
    for kind in ("deployments", "statefulsets"):
        for entry in (k8s(item, "GET", f"/apis/apps/v1/namespaces/{ns}/{kind}") or {}).get("items") or []:
            name = entry["metadata"]["name"]
            if (entry.get("spec") or {}).get("replicas"):
                continue
            k8s(item, "PATCH", f"/apis/apps/v1/namespaces/{ns}/{kind}/{name}", {"spec": {"replicas": mins.get(name, 1)}})


def rds_status(item, db):
    rds = aws("rds", item["region"])
    try:
        return rds.describe_db_instances(DBInstanceIdentifier=db)["DBInstances"][0]["DBInstanceStatus"]
    except rds.exceptions.DBInstanceNotFoundFault:
        return "not_found"


def rds_stop(item, db):
    if rds_status(item, db) != "available":
        return
    try:
        aws("rds", item["region"]).stop_db_instance(DBInstanceIdentifier=db)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "InvalidDBInstanceState":
            raise
        log(event="rds_stop_skipped", db=db, error=str(err))


def rds_start(item, db):
    if rds_status(item, db) != "stopped":
        return
    try:
        aws("rds", item["region"]).start_db_instance(DBInstanceIdentifier=db)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "InvalidDBInstanceState":
            raise
        log(event="rds_start_skipped", db=db, error=str(err))


def wait_rds(targets):
    pending = defaultdict(list)
    for item in targets:
        pending[item["region"]].extend(item["rds"])
    pending = {r: list(dict.fromkeys(ids)) for r, ids in pending.items() if ids}
    if not pending:
        return
    deadline = time.time() + RDS_WAIT
    while time.time() < deadline:
        left = {}
        for region, ids in pending.items():
            rds, still = aws("rds", region), []
            for db in ids:
                try:
                    status = rds.describe_db_instances(DBInstanceIdentifier=db)["DBInstances"][0]["DBInstanceStatus"]
                except rds.exceptions.DBInstanceNotFoundFault:
                    status = "not_found"
                if status == "stopped":
                    rds.start_db_instance(DBInstanceIdentifier=db)
                if status not in ("available", "not_found"):
                    still.append(db)
            if still:
                left[region] = still
        if not left:
            return
        pending = left
        time.sleep(RDS_POLL)
    raise TimeoutError(f"RDS did not reach available within {RDS_WAIT}s")


def _collect(errors, label, fn):
    try:
        fn()
    except Exception as err:
        errors.append(f"{label}: {err}")


def run_stop(targets):
    errors = []
    for item, name in parents_of(targets):
        _collect(errors, f"parent {name}", lambda n=name, i=item: pause(i, n))
    for item in targets:
        def _one(i=item):
            for app in i["apps"]:
                pause(i, app)
            delete_hpas(i)
            scale(i, 0)
            cron(i, True)
            for db in i["rds"]:
                rds_stop(i, db)
        _collect(errors, item["display"], _one)
    if errors:
        raise RuntimeError(" | ".join(errors))


def run_start_rds(targets):
    errors = []
    for item in targets:
        for db in item["rds"]:
            _collect(errors, f"{item['display']} rds {db}", lambda i=item, d=db: rds_start(i, d))
    if errors:
        raise RuntimeError(" | ".join(errors))


def run_start_apps(targets):
    errors = []
    for item in targets:
        for db in item["rds"]:
            _collect(errors, f"{item['display']} rds {db}", lambda i=item, d=db: rds_start(i, d))
    try:
        wait_rds(targets)
    except Exception as err:
        errors.append(f"wait_rds: {err}")
        raise RuntimeError(" | ".join(errors)) from err
    for item in targets:
        def _apps(i=item):
            for app in i["apps"]:
                unpause(i, app)
            cron(i, False)
        _collect(errors, item["display"], _apps)
    for item, name in parents_of(targets):
        _collect(errors, f"parent {name}", lambda n=name, i=item: unpause(i, n))
    for item in targets:
        _collect(errors, f"{item['display']} kick", lambda i=item: kick_from_zero(i))
    if errors:
        raise RuntimeError(" | ".join(errors))


def start_worker(action, keys, reason, user_id, phase="", retry=0):
    lambda_client.invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"], InvocationType="Event",
        Payload=json.dumps({
            "source": "worker", "action": action, "phase": phase or "",
            "namespace_keys": keys, "reason": reason, "user_id": user_id, "retry": retry,
        }).encode(),
    )


def handle_scheduled(event):
    action, phase = event.get("action"), event.get("phase") or ""
    if action not in ("STOP", "START"):
        return {"ok": False, "error": "action must be STOP or START"}
    rk = event.get("region_key") or ""
    if event.get("namespace_keys"):
        keys = [k for k in event["namespace_keys"] if k in TARGETS]
    elif rk:
        keys = keys_for(rk)
    else:
        keys = list(TARGETS)
    reason = event.get("reason") or CFG.get("schedule_reason") or "nightly schedule"
    by = defaultdict(list)
    for k in keys:
        by[TARGETS[k]["region_key"]].append(k)
    for group in by.values():
        start_worker(action, group, reason, "scheduler", phase)
    label = action if action == "STOP" else f"START ({phase or 'full'})"
    post(f"{label} started by nightly schedule\n*Namespaces:* {', '.join(TARGETS[k]['display'] for k in keys)}\n*Reason:* {reason}")
    return {"ok": True}


def handle_worker(event):
    action, phase = event.get("action"), event.get("phase") or ""
    keys = event.get("namespace_keys") or []
    reason, user_id = event.get("reason") or "-", event.get("user_id") or "unknown"
    retry = int(event.get("retry") or 0)
    targets = [TARGETS[k] for k in keys if k in TARGETS]
    shown = ", ".join(i["display"] for i in targets) or "(none)"
    who = "nightly schedule" if user_id == "scheduler" else f"<@{user_id}>"
    label = action if action == "STOP" else f"START ({phase or 'full'})"
    try:
        if action == "STOP":
            run_stop(targets)
        elif phase == "rds":
            run_start_rds(targets)
        else:
            run_start_apps(targets)
        post(f"{label} done\n*Namespaces:* {shown}\n*Requested by:* {who}\n*Reason:* {reason}")
        return {"ok": True}
    except TimeoutError as err:
        if action == "START" and phase != "rds" and retry < RDS_RETRIES:
            start_worker(action, keys, reason, user_id, phase or "apps", retry + 1)
            post(f"{label} waiting — RDS not ready. Retry {retry + 1}/{RDS_RETRIES}.\n*Namespaces:* {shown}")
            return {"ok": True, "retry": retry + 1}
        post(f"{label} FAILED — {shown}\nRequested by: {who}\nError: `{err}`")
        raise
    except Exception as err:
        post(f"{label} FAILED — {shown}\nRequested by: {who}\nError: `{err}`")
        raise


def handle_submit(payload):
    view = payload.get("view") or {}
    if view.get("callback_id") != "start_stop_request":
        return resp(400, {"error": "Unknown modal"})
    state = view.get("state") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    try:
        action = state_val(state, "action_block", "action")["value"]
        reason = state_val(state, "reason_block", "reason", field="value") or ""
        rk = (state_val(state, "region_block", "region") or {}).get("value") or meta.get("region_key")
        keys = [o["value"] for o in (state_val(state, f"ns_{rk}", "namespace", field="selected_options") or [])]
    except (KeyError, TypeError):
        return resp(200, {"response_action": "errors", "errors": {"region_block": "Select a region, then at least one namespace."}})
    keys = [k for k in keys if k in set(keys_for(rk or ""))]
    if action not in ("STOP", "START") or not keys:
        return resp(200, {"response_action": "errors", "errors": {f"ns_{rk}" if rk else "region_block": "Select a region first, then at least one namespace."}})
    start_worker(action, keys, reason, (payload.get("user") or {}).get("id") or "")
    return resp(200, {
        "response_action": "update",
        "view": {"type": "modal", "title": _t(CFG.get("modal_title") or "Env start/stop"), "close": _t("Done"), "blocks": [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{action} started for:\n`{', '.join(TARGETS[k]['display'] for k in keys)}`\nResult will be posted in the start-stop channel."},
        }]},
    })


def lambda_handler(event, context):
    if event.get("source") == "worker":
        return handle_worker(event)
    if event.get("source") == "scheduler":
        return handle_scheduled(event)
    if not verify_slack(event):
        return resp(401, {"error": "Unauthorized"})
    data = urllib.parse.parse_qs(body(event) or "")
    if "payload" in data:
        payload = json.loads(data.get("payload", ["{}"])[0])
        if payload.get("type") == "block_actions":
            return handle_region(payload)
        if payload.get("type") == "view_submission":
            return handle_submit(payload)
        return resp(200, {"ok": True})
    if data.get("command", [""])[0] in COMMANDS:
        opened = slack("views.open", {"trigger_id": data.get("trigger_id", [""])[0], "view": build_modal(data.get("channel_id", [""])[0])})
        if not opened.get("ok"):
            return resp(200, {"response_type": "ephemeral", "text": f"Could not open form: {opened.get('error')}"})
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": ""}
    return resp(200, {"response_type": "ephemeral", "text": CFG.get("help_text") or "Use the slash command to open the form."})
