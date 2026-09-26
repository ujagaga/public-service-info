# Application review — fixes implemented

The styling refresh covers sign-in, registration, address settings, and user
administration. The backend findings from that review are now addressed:

| Finding | Implemented fix |
| --- | --- |
| Registration could overwrite accounts using a browser-supplied email | Registration is bound to Google's verified email in an expiring signed session. Inserts reject duplicate emails without replacing accounts. |
| Approval credentials could be used as login tokens | Approval tokens are stored separately and consumed atomically. Account routes reject pending users. The migration preserves pending approval links and invalidates all legacy login tokens once. |
| Retries repeated successful notifications | Successful checks and pending emails are persisted atomically. Subsequent URL calls reuse daily results and retry only pending email or failed services; a shared process lock prevents overlap. |
| Partial failures returned HTTP 200 | Reports containing errors return HTTP 500. |
| Power announcements were assumed to be for tomorrow | The heading date must match tomorrow in Europe/Belgrade. Address matching includes the target date and requires the model to confirm it. |
| Model responses and water URLs were insufficiently validated | Required JSON values are type-checked, positive matches require details, and selected water URLs must exactly match parsed links on the configured origin. Only an exact `NEMA` is accepted as no announcement. |
| Admin and email approvals behaved differently | Both paths share the same atomic approval and email operation. Repeated approval requests cannot resend the confirmation or downgrade an administrator. |

Tokens now use cryptographically secure randomness. Login cookies are HttpOnly
and SameSite=Lax (Secure on HTTPS), and logout revokes the database token.

## Verification and limits

Offline regression tests cover the fixes, including an old-schema migration and
separate processes competing for the checker lock, four consecutive URL triggers,
and a fresh CGI process retrying saved emails without repeating Gemini calls. No live OAuth, Gemini, or SMTP
flow was run. The power date parser was checked against the date heading on the
[configured EDS announcement page](https://elektrodistribucija.rs/planirana-iskljucenja-srbija/NoviSad_Dan_1_Iskljucenja.htm).

The upgrade requires existing users to log in again. Restart all application and
checker workers together so no old code continues using the former token scheme.

SMTP acceptance and the SQLite commit cannot be atomic: a crash or ambiguous SMTP
failure between them can still cause a duplicate on retry. Delivery records cannot
retroactively identify mail sent by older incomplete runs. Date/address matching
still depends on the correctness of source announcements and Gemini's interpretation;
strict response validation does not eliminate semantic model mistakes.
