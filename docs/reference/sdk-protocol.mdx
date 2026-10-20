# Driving an OpenLolo box from any language

Scripts and AI agents use the same interface: the box's MCP endpoint over HTTPS. A script needs
three things: a grant (OAuth device authorization), a bearer token on each call, and, on the LAN,
a pinned certificate. The Python SDK (`packages/sdk-python`) wraps all of this. This page is for
writing a client in another language, or for calling the box with curl.

## Entry points

| Entry | Address | TLS |
|---|---|---|
| LAN | `https://openlolo-xxxx.local` (port in the box's `lan_url`) | Self-signed. Pin the SHA-256 of the DER leaf certificate. |
| Remote | `https://<box>.<tailnet>.ts.net` | Public CA. Verify normally. |

Grants are bound to the entry that issued them. A LAN token does not work remotely, and a remote
token does not work on the LAN.

## 1. Discover

```
GET /.well-known/oauth-authorization-server
```

Use `device_authorization_endpoint` and `token_endpoint` from the response. If the box does not
list `urn:ietf:params:oauth:grant-type:device_code` in `grant_types_supported`, it is too old for
scripts.

## 2. Ask for approval (RFC 8628)

```
POST /oauth/device_authorization
Content-Type: application/x-www-form-urlencoded

client_name=nightly.py+on+mac-mini&scope=phone:read+phone:control[&client_id=<cached id>]
```

```json
{"device_code": "…", "user_code": "BCDF-GHJK", "verification_uri": "http://openlolo-xxxx.local/access?r=…",
 "verification_uri_complete": "…", "expires_in": 600, "interval": 5, "client_id": "…"}
```

- Show the user: `Approve '<client_name>' on the box page (Access). Code <user_code>.` The owner
  approves on the box page, where the request appears with the same code. Nobody types the code.
- `client_id` is not part of RFC 8628. Keep it: refreshes need it, and sending it back on a later
  request reuses the same entry under Access instead of creating a new one.
- Scripts get `phone:read` and `phone:control` only. `owner:admin` is refused (`invalid_scope`).
- Limits: at most 5 pending script requests per box, and 10 requests per minute. Beyond either
  limit the box answers 429 with `slow_down`. A new request from the same `client_id` replaces
  that client's pending one.

## 3. Poll for the decision

```
POST /token
grant_type=urn:ietf:params:oauth:grant-type:device_code&device_code=…&client_id=…
```

| Answer | Meaning |
|---|---|
| 200 token response | Approved. |
| 400 `authorization_pending` | Waiting. Poll again after `interval` seconds. |
| 400 `slow_down` | Too fast. Add 5 s to the interval (the box caps it at 30 s). |
| 400 `access_denied` | The owner denied the request. |
| 400 `expired_token` | Lapsed after 10 minutes or 150 polls. Start again. |
| 400 `invalid_grant` | Unknown code, wrong `client_id`, or already used. Device codes are single use. |
| 429 `slow_down` | Too many failed polls box-wide. Back off. |

The token response is standard: an `access_token` that lasts 15 minutes, a rotating
`refresh_token`, and `scope`. The owner chooses how long the grant lasts:
- 24 hours.
- 30 days, renewed on every refresh.
- Until removed under Access.

## 4. Refresh

```
POST /token
grant_type=refresh_token&refresh_token=…&client_id=…
```

Every refresh returns a new refresh token. Presenting an old one revokes the whole grant, so a
client must:
- Store the new pair before using it.
- Serialize refreshes across processes. The Python SDK holds an exclusive file lock while it
  refreshes.

## 5. Call tools

The box serves MCP statelessly with JSON responses. A tool call is one POST, with no `initialize`
and no session:

```
POST /mcp
Authorization: Bearer <access_token>
Accept: application/json, text/event-stream
Content-Type: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "openlolo_launch_app", "arguments": {"bundle_id": "com.apple.mobilenotes"}}}
```

- Read `result.structuredContent` for data and `result.content[]` blocks of type `image` (base64)
  for screenshots. `result.isError` marks a refused or failed call. `structuredContent.error.code`
  holds the stable error code, and `dispatched: false` means nothing reached the phone.
- `tools/list` returns every tool with its JSON Schema. Use it to check that the box supports
  what you call.
- Mutating tools take an optional `operation_id` (a UUID). Send a fresh one per intended action,
  and send the same one only to retry after a dropped connection: the box runs each id at most
  once. If the retry fails too, ask `openlolo_operation_status` instead of replaying.
- Touch tools need `frame_id` and `geometry_epoch` from the last image you received, with
  coordinates as fractions of that image. The box refuses a frame that is no longer current
  (`FRAME_EXPIRED`) or older than 120 s.
- Limits:
  - Request bodies: at most 64 KiB.
  - 120 calls per minute per client. `RATE_LIMITED` means the call did not run.
  - Control lapses after 90 s without activity. Call `openlolo_control` with `command=release`
    when done.

## curl walkthrough (remote entry)

```sh
BOX=https://pi.tailnet.ts.net
curl -s -X POST $BOX/oauth/device_authorization -d client_name=curl -d 'scope=phone:read phone:control'
# Approve on the box page, then:
curl -s -X POST $BOX/token -d grant_type=urn:ietf:params:oauth:grant-type:device_code \
     -d device_code=$DEVICE_CODE -d client_id=$CLIENT_ID
curl -s $BOX/mcp -H "Authorization: Bearer $ACCESS" -H 'Accept: application/json, text/event-stream' \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"openlolo_status","arguments":{}}}'
```

On the LAN, replace `-k`-style blind trust with a pinned certificate. curl has no SHA-256 leaf pin
for self-signed certificates, so either use `openlolo-sdk call <box> <tool> key=value`, which pins,
or compare the fingerprint first:

```sh
openssl s_client -connect openlolo-xxxx.local:443 </dev/null 2>/dev/null \
  | openssl x509 -outform der | shasum -a 256
```
