"""Bridge between AWS Step Functions and AWS DevOps Agent directed actions.

Sample code for demonstration only. Not production ready.

Actions (field "action" of the event):
  ask     {"prompt": "..."}                                   start a chat, send the request
  check   {"pending": {...}}                                  is this approval covered by an auto-approve rule?
  notify  {"taskToken": "...", "pending": {...}}              send the approval request to a human (SNS)
  decide  {"executionId": "...", "pending": {...}, "decision": "APPROVED"|"REJECTED"}

ask/decide return {"executionId", "status": "PENDING_APPROVAL"|"DONE"|"FAILED", "pending", "error", "text"}.

Environment variables:
  AGENT_SPACE_ID       required
  CHAT_USER_ID         optional, default "sfn-directed-actions"
  APPROVAL_TOPIC_ARN   optional, SNS topic that receives approval requests (human path)
  AUTO_APPROVE_RULES   optional, JSON list of rules, see README. Empty = never auto-approve.
"""

import json
import os
import re

import boto3

SPACE = os.environ["AGENT_SPACE_ID"]
USER = os.environ.get("CHAT_USER_ID", "sfn-directed-actions")
TOPIC = os.environ.get("APPROVAL_TOPIC_ARN", "")
RULES = json.loads(os.environ.get("AUTO_APPROVE_RULES", "[]") or "[]")

agent = boto3.client("devops-agent")


def _read(resp):
    """Consume the SendMessage event stream. Return (text, pending approval or None, error)."""
    blocks, text, failed = {}, [], None
    for ev in resp["events"]:
        kind = next(iter(ev))
        e = ev[kind]
        if kind == "contentBlockStart":
            blocks[e["index"]] = [e.get("type"), ""]
        elif kind == "contentBlockDelta":
            d = e["delta"]
            if "textDelta" in d:
                text.append(d["textDelta"].get("text", ""))
            elif "jsonDelta" in d and e["index"] in blocks:
                blocks[e["index"]][1] += d["jsonDelta"].get("partialJson", "")
        elif kind == "responseFailed":
            failed = f"{e.get('errorCode')}: {e.get('errorMessage')}"

    pending = None
    for btype, raw in blocks.values():
        if btype != "action_approval" or not raw:
            continue
        data = json.loads(raw).get("data", {})
        pending = {
            "tool": data["tool"],
            "toolUseId": data["tool_use_id"],
            "approvalId": data["approval_id"],
            "argumentPins": data["options"][0]["argument_pins"],
            "summary": data.get("structured_summary", {}).get(
                "human_readable_summary", ""
            ),
            "role": data.get("role_arn"),
        }
    if pending:
        allraw = "".join(raw for _, raw in blocks.values())
        m = re.search(
            r"v1:before_tool_call:" + re.escape(pending["toolUseId"]) + r":[0-9a-f-]+",
            allraw,
        )
        if not m:
            failed = failed or "interruptId not found in the stream"
        else:
            pending["interruptId"] = m.group(0)
    return "".join(text)[-1500:], pending, failed


def _result(execution_id, text, pending, failed):
    if failed:
        status = "FAILED"
    elif pending:
        status = "PENDING_APPROVAL"
    else:
        status = "DONE"
    return {
        "executionId": execution_id,
        "status": status,
        "pending": pending or {},
        "error": failed or "",
        "text": text,
    }


def matches(rule, pins):
    """A rule matches when every key it names has the same value in the pins.

    Special key "KeyPrefix": the pin "Key" must start with this value.
    """
    for key, want in rule.items():
        if key == "KeyPrefix":
            if not str(pins.get("Key", "")).startswith(want):
                return False
        elif str(pins.get(key)) != str(want):
            return False
    return True


def lambda_handler(event, _ctx):
    action = event["action"]

    if action == "ask":
        ex = agent.create_chat(agentSpaceId=SPACE, userId=USER, userType="IAM")[
            "executionId"
        ]
        resp = agent.send_message(
            agentSpaceId=SPACE, executionId=ex, content=event["prompt"], userId=USER
        )
        return _result(ex, *_read(resp))

    if action == "check":
        pins = event["pending"].get("argumentPins", {})
        rule = next((r for r in RULES if matches(r, pins)), None)
        print(
            json.dumps({"auto_approve": bool(rule), "operation": pins.get("operation")})
        )
        return {"auto": bool(rule)}

    if action == "notify":
        p = event["pending"]
        if not TOPIC:
            raise RuntimeError(
                "APPROVAL_TOPIC_ARN is not set: no channel to reach a human"
            )
        # The task token alone is enough to approve: send it only to the approval channel.
        boto3.client("sns").publish(
            TopicArn=TOPIC,
            Subject="DevOps Agent approval request",
            Message=json.dumps(
                {
                    "request": p["summary"],
                    "operation": p["argumentPins"].get("operation"),
                    "parameters": p["argumentPins"],
                    "role": p["role"],
                    "taskToken": event["taskToken"],
                },
                indent=1,
            ),
        )
        print(
            json.dumps(
                {"notified": True, "operation": p["argumentPins"].get("operation")}
            )
        )
        return {"notified": True}

    if action == "decide":
        p, ex, decision = event["pending"], event["executionId"], event["decision"]
        kwargs = dict(agentSpaceId=SPACE, approvalId=p["approvalId"], action=decision)
        if decision == "APPROVED":
            # Approve exactly what the agent asked for, once.
            kwargs.update(
                singleUse=True,
                finalPattern={"tool": p["tool"], "argumentPins": p["argumentPins"]},
            )
        else:
            kwargs["reason"] = event.get("reason", "rejected")
        agent.update_approval_action(**kwargs)
        resp = agent.send_message(
            agentSpaceId=SPACE,
            executionId=ex,
            content="approval decision",
            userId=USER,
            context={
                "userActionResponse": "APPROVAL_ACTION",
                "approvalAction": {
                    "toolUseId": p["toolUseId"],
                    "interruptId": p["interruptId"],
                    "approvalId": p["approvalId"],
                    "action": decision,
                    "buttonText": "Approve" if decision == "APPROVED" else "Deny",
                },
            },
        )
        return _result(ex, *_read(resp))

    raise ValueError(f"unknown action {action}")
