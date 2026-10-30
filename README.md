# AWS DevOps Agent directed actions with AWS Step Functions

Sample code for demonstration only. Not production ready: review, test and harden it before any real use.

This sample drives an AWS DevOps Agent chat from AWS Step Functions, so that a workflow can ask the agent to run a write operation (a "directed action") and handle the approval:

- either **automatically**, when the request matches one of your auto-approve rules,
- or by **waiting for a human** (task token callback), notified through Amazon SNS.

The example use case: the agent runs a Network Manager route analysis (`networkmanager:StartRouteAnalysis`), reads the result, and writes a markdown report to Amazon S3.

## How it works

```
AskAgent -> NeedsApproval -> CheckAutoApprove -> AutoOrHuman
                ^                                  |        \
                |                           AutoApprove   WaitForHuman (task token, SNS)
                |                                  |        /
                +------------------------- SendDecision <--+
```

| Step | What it does |
|---|---|
| AskAgent | Lambda: `CreateChat`, then `SendMessage` with the prompt. Reads the approval request from the response stream. |
| NeedsApproval | Approval requested? Go on. Agent done? Succeed. Error? Fail. |
| CheckAutoApprove | Lambda: does the request (operation + parameters) match a rule in `AUTO_APPROVE_RULES`? |
| AutoApprove | Rule matched: decision = APPROVED, no human. |
| WaitForHuman | No rule matched: the workflow pauses and publishes the request and the task token to SNS. Someone calls `SendTaskSuccess` with `{"decision":"APPROVED"}` or `{"decision":"REJECTED"}`. |
| SendDecision | Lambda: `UpdateApprovalAction` (single use, exactly the parameters the agent asked for), then `SendMessage` to resume the chat. |

The loop repeats for each write. In the example there are 2: the route analysis, then the S3 report.

A Lambda is needed because Step Functions supports `aws-sdk:devopsagent` but not `SendMessage`, which returns an event stream ([supported SDK integrations](https://docs.aws.amazon.com/step-functions/latest/dg/supported-services-awssdk.html)).

## Prerequisites

1. Agent Actions (directed actions) enabled on the Agent Space.
2. An Agent Actions role registered on the account association, with status `valid`:
   - trust policy: [iam/agent-actions-role-trust.json](iam/agent-actions-role-trust.json) (the 3 STS actions are required, and `aws:SourceAccount` is the account that owns the Agent Space),
   - permissions: [iam/agent-actions-role-permissions.json](iam/agent-actions-role-permissions.json).
3. On the Agent Space primary (read) role, add [iam/primary-role-extra-read.json](iam/primary-role-extra-read.json): `AIDevOpsAgentAccessPolicy` does not include `networkmanager:GetRouteAnalysis`.
4. A report bucket, and an SNS topic for the human path.

Docs: [Working with directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html), [Limiting agent access in an AWS account](https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security-limiting-agent-access-in-an-aws-account.html).

## Deploy

Replace the `${...}` placeholders in `iam/*.json`, `statemachine.asl.json` and `examples/*` first.

1. Lambda package. The boto3 in the Lambda runtime may not have the DevOps Agent chat APIs yet, so bundle a recent one:

   ```
   pip install --target build --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.13 boto3
   cp lambda/handler.py build/
   (cd build && zip -r ../bridge.zip .)
   ```

2. Create the Lambda role ([trust](iam/lambda-role-trust.json), [permissions](iam/lambda-role-permissions.json)) and the function: runtime `python3.13`, handler `handler.lambda_handler`, timeout 900 s.
   Environment variables:

   | Variable | Value |
   |---|---|
   | `AGENT_SPACE_ID` | your Agent Space id |
   | `APPROVAL_TOPIC_ARN` | SNS topic for approval requests (human path) |
   | `AUTO_APPROVE_RULES` | JSON list of rules, see below. Empty = always ask a human. |

3. Create the Step Functions role ([trust](iam/stepfunctions-role-trust.json), [permissions](iam/stepfunctions-role-permissions.json)) and the state machine from [statemachine.asl.json](statemachine.asl.json).

4. Start an execution with `{"prompt": "..."}`. See [examples/prompt.txt](examples/prompt.txt).

## Auto-approve rules

`AUTO_APPROVE_RULES` is a JSON list. A rule matches when every key it names has the same value in the approval request parameters. `KeyPrefix` is special: the `Key` parameter must start with it. Example: [examples/auto-approve-rules.json](examples/auto-approve-rules.json).

```json
[
  {"operation": "networkmanager:start_route_analysis", "GlobalNetworkId": "global-network-xxxx"},
  {"operation": "s3:put_object", "Bucket": "my-report-bucket", "KeyPrefix": "reports/"}
]
```

Anything that matches no rule goes to the human path. If `APPROVAL_TOPIC_ARN` is not set, the execution fails instead of approving.

The Agent Actions role is the real limit: the agent can never do more than this role allows, even with an auto-approval. Keep the role as narrow as the rules.

## Human path

The SNS message contains the operation, its parameters, the role and the task token. Your channel (email, chat bot, ticket) shows the request and sends the answer back:

```
aws stepfunctions send-task-success --task-token "<token>" --task-output '{"decision":"APPROVED"}'
```

## Things to know

- **The task token is enough to resume the workflow.** Send it only to the approval channel. Never log it.
- **Approvals only happen in chat** (console or API). In an autonomous investigation, a write fails because nobody can approve it. This sample uses the chat API.
- **CloudTrail attribution.** Approved in the console, the action carries the operator's identity. Approved through the API, it carries the identity that calls `UpdateApprovalAction` (here the Lambda role). Record who decided in your workflow.
- **Write precise prompts.** With a vague request, the agent may ask a clarifying question instead of requesting an approval. The workflow then ends with no action.
- **Network Manager is called in `us-west-2`**, its home Region. A Service Control Policy that restricts Regions can block it.
- **`s3:ListAllMyBuckets`.** In testing, the agent checked the bucket owner before writing, and refused to write without this permission.
- **Event stream format.** The Lambda parses content blocks of type `action_approval` and the interrupt id from the `SendMessage` stream. These details come from testing, not from the API reference, and may change.

## Tested

Tested in a single AWS account (Agent Space, network and bucket in the same account):

| Case | Result |
|---|---|
| Human path, 2 approvals | execution succeeded, report written |
| Auto-approve, both writes match a rule | execution succeeded, report written, no human |
| Auto-approve, S3 key outside the rule | route analysis auto-approved, S3 write sent to the human path |

Not tested: cross-account setups (Agent Space and target resources in different accounts), the SNS channel end to end.

## License

MIT-0. See [LICENSE](LICENSE).
