# Uhlexa: the commercial (target 2:45, hard limit under 3:00)

**Ken's direction (2026-10-22):** a commercial of everyday life showing all the ways Uhlexa helps as a fully capable AI
assistant, including A2A tasks with friends' and family's agents. **No logos and no screens are ever seen**: no screen
recordings, no app chrome, no vendor names or dashboards. Uhlexa appears only the way she would in a real home: her
face and captions on a kitchen tablet, the speaker's light ring, a phone that buzzes or rings, her voice.

**Rules this must still meet** (official Devpost rules): under 3:00; public on YouTube or Vimeo; English; "footage that
shows the Project functioning"; for a simulated Alexa+ entry "the demo video must clearly show the simulated
experience"; no third-party trademarks or copyrighted music without permission. Judges may judge from this video
alone. In-world shots of Uhlexa's own surfaces satisfy "shows it functioning" without any screen recording.

## One video, live action first

Devpost takes **one** video link, under 3:00. Everything that shows Uhlexa doing something is **live action, filmed
for real**: Ken on his own phone taking and making real calls, real texts arriving, the real tablet answering in a real
room. That is the proof the phone and everything else really work. Generated footage (if used at all) is **mood b-roll
only**: establishing shots, transitions, the city waking up. It never shows Uhlexa, a phone screen, a call or a result,
so nothing a judge sees as "it works" is synthetic. Mark any generated shot in the edit notes; if in doubt, leave it out.

Shoot list per beat: one wide (people and room), one close (the phone ringing or being answered, the tablet face), and
clean room audio of the call on speaker so judges hear Uhlexa's side.

## Cast and props

- **Ken** (the household's guardian; his agent is "Ken's Uhlexa" or the seeded "Uhlexa", Ken's choice).
- **Sam**, a friend or family member (adult, they/them, agreed in advance to AI texts and calls). Their own Uhlexa
  is the other agent in the A2A beats. Swap the name for the real helper's.
- **Kitchen tablet** = the hub ("Echo Show"): `/voice/hub` full-screen in kiosk mode, so no browser bar, tabs or
  bookmarks are ever in frame. Uhlexa's face, captions and cards are the only things on it.
- **Small speaker stand-in** = `/voice/echo` on a phone or small tablet, kiosk mode: the light ring and caption line.
- **Two phones** (Ken's, Sam's): shown buzzing and ringing, held naturally. Message text appears as **our own caption
  overlay**, never the phone's messaging app.
- No music, or audio Ken made or holds a licence for. Room sound and voices carry it.

## The commercial

| Time | Scene (live action) | Uhlexa does | Feature it proves |
|---|---|---|---|
| 0:00-0:10 | **Cold open, morning kitchen.** Coffee brewing. Ken, half awake: "Uhlexa, what's on for today?" Her face on the tablet lights up and answers; captions under her. | Answers from the family's own threads and tasks, not the internet | Hub voice turn, Keiki agent as brain, Uhlexa MCP tools |
| 0:10-0:25 | Ken keeps talking **without saying her name again**: "Add milk and eggs to Sunday groceries." She confirms. Text overlay: *No wake word needed for a follow-up.* | Follow-up window; adds to the list | Wake-word setting (`follow_up`) |
| 0:25-0:45 | Tablet shows the grocery card: *started by Sam's Uhlexa · continued by Ken's Uhlexa*. Ken smiles: "Sam already started it?" Uhlexa: "Sam's Uhlexa opened it Tuesday. I added yours." | Resumes another agent's thread instead of starting over | Cross-agent persistent context (the core idea) |
| 0:45-1:05 | **Ken leaving the house, phone in hand.** It buzzes: "Uhlexa wants to order on the family grocery page. Approve?" Ken taps Approve, pockets the phone. Overlay: *She asks before she acts.* | Real-world action waits for a human; the agent can't approve itself | Human-only approvals, policy engine |
| 1:05-1:20 | Cut back to the empty kitchen: the tablet card ticks through steps and lands on *Verified*. | Runs the task in a real cloud browser and checks the result | Maritime browser task (or honest takeover) |
| 1:20-1:45 | **In the car (parked) / on a walk.** Ken: "Uhlexa, ask Sam's Uhlexa to bring dessert Sunday, and see if their mom's coming." Overlay: *Agent to agent.* | Ken's agent hands a task to Sam's agent | A2A handoff between agents |
| 1:45-2:05 | **Sam at work.** Sam's phone rings. Sam answers. We hear Sam's Uhlexa open with its AI and transcription notice, then: "Ken's Uhlexa asked if you can bring dessert Sunday." Sam: "Tell him yes, and Mom's in." | Sam's own agent calls Sam, takes the answer | Real outbound phone call, AI disclosure |
| 2:05-2:20 | Ken's phone buzzes; overlay of the message: *Sam's Uhlexa: Sam's bringing dessert. Their mom is coming.* The tablet at home updates the Sunday card: *Dessert: Sam ✓ · Guests: +1*. | Handoff completes and flows back to every surface | A2A completion, shared state |
| 2:20-2:35 | **Evening, kitchen.** Sam calls Ken's Uhlexa from their own phone: "Hey, what time is dinner Sunday?" Ken's Uhlexa answers from the same plan. | Answers a real inbound call with the family's live context | Inbound phone call, same thread state |
| 2:35-2:45 | Ken at the counter: "Thanks, Uhlexa." Her face smiles. Overlay, one line: *Uhlexa. A personal agent for everyone in your life. Simulated Alexa+ experience on a real MCP server.* | | Closing claim |

Voiceover: none needed; the people and Uhlexa carry it. If time is tight, cut 1:05-1:20 first, then 2:20-2:35.

## What each beat needs from the build (resolve before recording)

| Beat | Needs | Status (2026-10-23) |
|---|---|---|
| Hub/echo voice, wake-word follow-up, Uhlexa face | Voice worker, LiveKit, Runway face | Built and verified live (TASKS.md voice section); one real-mic take still open |
| Answers from family data | Keiki agent's MCP connection to `/mcp` | Connected for both agents (2026-10-23); tool turns were slow when the board was large, now 15 queries |
| Thread resumed by another agent | Seeded thread + routing + card label | Built and verified (resumed/started-by cards on the board) |
| Approval on Ken's phone | Approval notification on a phone surface | **NOT BUILT: no approval notifications.** Approve by tap on the app, hub or Family Board, or (flag, off) a typed one-time code. Show the tap, cut the buzz. |
| Browser task to *Verified* | Maritime runner | Built and run live once, but `ENABLE_MARITIME` is off in the demo: today the card reads *unavailable*. Cut 1:05-1:20 unless Ken turns it on and rehearses it. |
| **Agent-to-agent with a friend's agent** | `handoff_task` across two **different households** | **Gap: handoffs work only inside one household** (`handoff_task` between Alex's and Katie's agents is built; a friend in another household is not) (addendum §21.4, §21.8; test `..._different_household` denies it). Needs a cross-household rule, e.g. a mutual "trusted contacts" link both people opt into. Build-session decision for Ken. |
| Sam's agent calls Sam / Sam calls Ken's agent | Outbound call + inbound call | **Outbound calls are NOT BUILT** (an outbound call is refused). Inbound calls are built (AI + transcription greeting); a live call into the app is not yet verified, so test one before recording. Cut 1:45-2:05. |
| Message overlays | Our own captions in the edit (no phone UI shown) | Edit-time only |

If a beat isn't real by recording time, cut it rather than fake it: the rules say the video must show the project
functioning, and every claim here has to be true.

## Recording checklist

1. `scripts/run_public.sh` and `scripts/voice_worker.sh dev` running on the Mac; `GET /health` 200; **Reset demo**.
2. Tablet and speaker pages in kiosk/full-screen mode (Chrome: `--kiosk` or Presentation mode). Check every frame for
   browser chrome, notifications, dock or menu bar: none may be visible.
3. Only one hub tab open (Runway allows one live face at a time).
4. Mute any real Echo in the room. Say "Uhlexa", never "Alexa".
5. Phones: do-not-disturb except the demo contacts; lock-screen previews off so no app UI or number shows when filmed.
6. Sam has agreed in person to AI texts and calls (and any texting opt-in the line needs).
7. Captions on for every spoken line (burned in or as a YouTube caption track).
8. Record each beat as its own clean take so any one can be swapped or cut.

## Fallback order (if only ~60 seconds work)

0:00-0:25 morning voice + follow-up → 0:25-0:45 resumed thread → 0:45-1:05 approval tap → 1:20-2:05 A2A ask and the
friend's answer on the call → closing line.
