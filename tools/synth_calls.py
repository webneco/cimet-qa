#!/usr/bin/env python3
"""Synthetic Retailer 1 sales calls with hand-labelled ground truth.

    python tools/synth_calls.py            write data/synth/ (leads, scripts, transcripts, truth)

Each scenario is a short call (3-5 minutes) built from the Retailer 1 checklist,
with one or two defects planted on purpose. The same script drives three things:

  data/synth/scripts/<lead>.json       what each speaker says - input to make_audio.py
  data/synth/transcripts/<lead>.json   a reference transcript in the fixture format,
                                       so the eval runs offline with no audio and no ASR
  data/synth/truth.json                the auditor's answer for every A/B check and the
                                       gate, written by hand below, NOT computed from the
                                       scorer - that is what makes it a test

Every name, address, email, NMI and card number here is invented. The card is the
standard Visa test number. No real customer data goes anywhere near this.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "synth"

AGENT, CUST = "agent", "customer"

PLANS = {
    "R1-SAVER-FLEX": {"name": "Saver Flex", "rate_words": "thirty-one point nine", "rate_num": "31.9",
                      "supply": "ninety-eight point four five"},
    "R1-SECURE-12": {"name": "Secure 12", "rate_words": "twenty-nine point seven", "rate_num": "29.7",
                     "supply": "one hundred and four point nine"},
}

DISCLAIMER = ("Hi, you're speaking with {agent} from Retailer One. Before we go any further I need to let "
              "you know that this call is being recorded for quality assurance and training purposes. "
              "Do I have your consent to continue on a recorded line?")
IDENTITY = ("Thank you. Can I just confirm I'm speaking with the account holder, {cust}, and that you're "
            "authorised to make changes to this account?")
DMO = ("The Default Market Offer is the reference price set by the Australian Energy Regulator. The "
       "estimated annual cost of this plan is {pct} per cent less than the reference price for an average "
       "household in your distribution zone. This is an estimate only, and your actual cost will depend "
       "on your usage.")


def turn(speaker: str, text: str, pause: float = 0.7, *, ref_text: str | None = None,
         asr: float | None = None, overlap: float = 0.0) -> dict:
    """One line of the script.

    text      what is actually spoken (TTS input)
    ref_text  what the reference transcript shows, when it differs (e.g. crosstalk markers)
    pause     silence before this line; overlap starts it before the previous line ends
    """
    return {"speaker": speaker, "text": text, "pause": pause, "ref_text": ref_text,
            "asr": asr, "overlap": overlap}


# ---------------------------------------------------------------------- sections

def build_call(p: dict) -> list[dict]:
    plan = PLANS[p["plan_id"]]
    agent, cust, first = p["agent"], p["cust"], p["cust"].split()[0]
    t: list[dict] = []

    # 1. opening
    t.append(turn(AGENT, p.get("disclaimer", DISCLAIMER).format(agent=agent.split()[0]), 0.3))
    t.append(turn(CUST, "Yeah, that's fine, go ahead."))
    t += p.get("identity_turns") or [
        turn(AGENT, IDENTITY.format(cust=cust)),
        turn(CUST, f"Yes, that's me. {cust}, the account's in my name."),
    ]
    t.append(turn(AGENT, f"Perfect, thanks {first}. And the supply address is {p['address_spoken']}?"))
    t.append(turn(CUST, "That's the one."))

    # 2. disclosure
    t.append(turn(AGENT, f"Great. So this is about the {plan['name']} plan. Before any numbers there's a "
                         "short statement I'm required to read to you in full."))
    t.append(turn(CUST, "No worries."))
    dmo_text = p.get("dmo_text", DMO.format(pct=p.get("dmo_pct", "four")))
    t.append(turn(AGENT, dmo_text, ref_text=p.get("dmo_ref_text"), asr=p.get("dmo_asr")))
    if p.get("dmo_crosstalk"):
        t.append(turn(CUST, "Sorry, the line dropped out there, what was that?", overlap=2.0, asr=0.66))
        t.append(turn(AGENT, "No problem, I'll carry on and it'll all be in the documents as well."))
    else:
        t.append(turn(CUST, "Okay, got it."))

    # 3. rates and charges
    t.append(turn(AGENT, "A couple of quick questions first. Does anyone at the property rely on life "
                         "support equipment, and do you hold a concession card?"))
    t.append(turn(CUST, "No to both."))
    if p.get("dead_air"):
        t.append(turn(AGENT, "Let me pull the pricing up, bear with me, the system's slow today."))
        t.append(turn(CUST, "Sure.", asr=0.9))
        t.append(turn(AGENT, "Sorry about the wait, thanks for holding.", pause=p["dead_air"]))
    else:
        t.append(turn(AGENT, "Let me pull the pricing up for you."))
    t.append(turn(AGENT, p.get("rate_line", f"On the {plan['name']} plan your usage rate is "
                                            f"{p.get('rate_spoken', plan['rate_num'])} cents per kilowatt "
                                            f"hour including GST, and the daily supply charge is "
                                            f"{plan['supply']} cents a day."), 0.8))
    if p.get("talk_over"):
        t.append(turn(CUST, "Hang on, is that including GST?", overlap=1.5))
        t.append(turn(AGENT, "Yes, including GST.", overlap=0.8))
        t.append(turn(CUST, "Sorry, I talked over you, go on.", overlap=0.6))
        t.append(turn(AGENT, "No worries at all.", overlap=0.5))
    else:
        t.append(turn(CUST, "That sounds reasonable."))
    t.append(turn(AGENT, "The rate isn't locked, we'd give you written notice before any change and you "
                         "could leave then with no exit fee. Are you happy for me to go ahead?"))
    t.append(turn(CUST, "Yes, let's do it."))

    # payment, optionally with a card read out (must be redacted at ingest)
    if p.get("card_spoken"):
        t.append(turn(AGENT, "Would you like to pay by direct debit?"))
        t.append(turn(CUST, f"Yes, off my card. It's {p['card_spoken']}, expiry oh eight twenty nine."))
        t.append(turn(AGENT, "Thank you, that's gone straight into the payment gateway."))

    # email
    t.append(turn(AGENT, "What's the best email for your welcome pack and contract? The cooling-off "
                         "notice goes there too."))
    t.append(turn(CUST, p["email_spoken"], ref_text=p.get("email_ref_text"), asr=p.get("email_asr")))
    t.append(turn(AGENT, "Lovely, got it."))

    # 4. site details
    t.append(turn(AGENT, f"And just to confirm the NMI off your bill, I've got {p['nmi_spoken']}. "
                         "Does that match?"))
    t.append(turn(CUST, "Yes, that matches."))
    t.append(turn(AGENT, p.get("fuel_line", "And this is electricity only, you're not bringing gas across?")))
    t.append(turn(CUST, p.get("fuel_answer", "Electricity only.")))

    # close
    t.append(turn(AGENT, "That's everything. You'll get the welcome pack by email within one business day "
                         "and your ten business day cooling-off period starts when it lands."))
    t.append(turn(CUST, f"Thanks {agent.split()[0]}, bye."))
    return t


def digits_spoken(digits: str) -> str:
    words = "zero one two three four five six seven eight nine".split()
    return " ".join(words[int(d)] for d in digits)


# --------------------------------------------------------------------- scenarios
# `truth` is the auditor's verdict. Unlisted A/B checks are "pass".

SCENARIOS: list[dict] = [
    {
        "lead_id": "3613801", "title": "Clean compliant call",
        "params": {"plan_id": "R1-SECURE-12", "agent": "Priya Kaur", "cust": "Tom Becker",
                   "address_spoken": "14 Lygon Street, Carlton, Victoria, three zero five three",
                   "dmo_pct": "seven",
                   "email_spoken": "It's tom dot becker at outlook dot com.",
                   "nmi_spoken": "6 1 0 3 0 1 1 2 2 3 3"},
        "crm": {"email": "tom.becker@outlook.com", "nmi": "61030112233"},
        "truth": {},
    },
    {
        "lead_id": "3613802", "title": "Wrong rate quoted (27.9 vs plan 31.9)",
        "params": {"plan_id": "R1-SAVER-FLEX", "agent": "Daniel Reyes", "cust": "Aisha Rahman",
                   "address_spoken": "3 Bay Road, Sandringham, Victoria, three one nine one",
                   "rate_spoken": "27.9",
                   "email_spoken": "Use aisha dot rahman at gmail dot com.",
                   "nmi_spoken": "6 1 0 2 0 5 5 6 6 7 7"},
        "crm": {"email": "aisha.rahman@gmail.com", "nmi": "61020556677"},
        "truth": {"rates_and_charges": "fail"},
    },
    {
        "lead_id": "3613803", "title": "Email keyed wrong in CRM",
        "params": {"plan_id": "R1-SECURE-12", "agent": "Priya Kaur", "cust": "Liam O'Connor",
                   "address_spoken": "22 High Street, Northcote, Victoria, three zero seven zero",
                   "dmo_pct": "seven",
                   "email_spoken": "It's liam dot oconnor at bigpond dot com.",
                   "nmi_spoken": "6 1 0 3 4 4 5 5 6 6 7"},
        "crm": {"email": "liam.oconnor@bigpnd.com", "nmi": "61034455667"},
        "truth": {"email_captured": "fail"},
    },
    {
        "lead_id": "3613804", "title": "Recording disclaimer skipped",
        "params": {"plan_id": "R1-SAVER-FLEX", "agent": "Marcus Lee", "cust": "Grace Palmer",
                   "disclaimer": "Hi, it's {agent} from Retailer One, I'm just following up on the energy "
                                 "plan you looked at online. Have you got a few minutes?",
                   "address_spoken": "9 Grey Street, St Kilda, Victoria, three one eight two",
                   "email_spoken": "grace dot palmer at icloud dot com.",
                   "nmi_spoken": "6 1 0 2 7 7 8 8 9 9 0"},
        "crm": {"email": "grace.palmer@icloud.com", "nmi": "61027788990"},
        "truth": {"recording_disclaimer": "fail"},
    },
    {
        "lead_id": "3613805", "title": "DMO statement paraphrased",
        "params": {"plan_id": "R1-SAVER-FLEX", "agent": "Daniel Reyes", "cust": "Sophie Tran",
                   "address_spoken": "51 Main Road, Eltham, Victoria, three zero nine five",
                   "dmo_text": "Basically this plan works out a bit cheaper than the government's standard "
                               "price, around four per cent or so, so you should be saving money.",
                   "email_spoken": "It's sophie dot tran at gmail dot com.",
                   "nmi_spoken": "6 1 0 2 1 2 3 4 5 6 7"},
        "crm": {"email": "sophie.tran@gmail.com", "nmi": "61021234567"},
        "truth": {"dmo_verbatim": "fail"},
    },
    {
        "lead_id": "3613806", "title": "Account holder authority never confirmed",
        "params": {"plan_id": "R1-SECURE-12", "agent": "Marcus Lee", "cust": "Helen Brooks",
                   "identity_turns": [
                       turn(AGENT, "Thanks. Who am I speaking with today?"),
                       turn(CUST, "It's Mark, I'm Helen's husband. She asked me to sort the power out."),
                       turn(AGENT, "No problem Mark, let's get it done then."),
                   ],
                   "address_spoken": "6 Wattle Grove, Box Hill, Victoria, three one two eight",
                   "dmo_pct": "seven",
                   "email_spoken": "Use helen dot brooks at outlook dot com.",
                   "nmi_spoken": "6 1 0 3 9 8 7 6 5 4 3"},
        "crm": {"email": "helen.brooks@outlook.com", "nmi": "61039876543"},
        "truth": {"account_holder": "fail"},
    },
    {
        "lead_id": "3613807", "title": "Card number read aloud (must be redacted), otherwise clean",
        "params": {"plan_id": "R1-SAVER-FLEX", "agent": "Priya Kaur", "cust": "Ben Carter",
                   "address_spoken": "17 Station Street, Fairfield, Victoria, three zero seven eight",
                   "card_spoken": digits_spoken("4111111111111111"),
                   "email_spoken": "It's ben dot carter at gmail dot com.",
                   "nmi_spoken": "6 1 0 2 4 4 3 3 2 2 1"},
        "crm": {"email": "ben.carter@gmail.com", "nmi": "61024433221"},
        "truth": {},
    },
    {
        "lead_id": "3613808", "title": "Dead air and talk-over, otherwise clean",
        "params": {"plan_id": "R1-SECURE-12", "agent": "Daniel Reyes", "cust": "Nina Petrova",
                   "address_spoken": "40 Church Street, Richmond, Victoria, three one two one",
                   "dmo_pct": "seven", "dead_air": 24.0, "talk_over": True,
                   "email_spoken": "nina dot petrova at yahoo dot com.",
                   "nmi_spoken": "6 1 0 3 2 2 1 1 0 0 9"},
        "crm": {"email": "nina.petrova@yahoo.com", "nmi": "61032211009"},
        "truth": {},
    },
    {
        "lead_id": "3613809", "title": "NMI read-back mismatch (non-blocking)",
        "params": {"plan_id": "R1-SAVER-FLEX", "agent": "Marcus Lee", "cust": "Oliver Grant",
                   "address_spoken": "8 Park Lane, Brighton, Victoria, three one eight six",
                   "email_spoken": "It's oliver dot grant at gmail dot com.",
                   "nmi_spoken": "6 1 0 2 8 8 1 1 4 4 5"},
        "crm": {"email": "oliver.grant@gmail.com", "nmi": "61028811454"},
        "truth": {"nmi": "fail"},
    },
    {
        "lead_id": "3613810", "title": "Crosstalk over the DMO - said, but not clearly heard",
        "params": {"plan_id": "R1-SAVER-FLEX", "agent": "Daniel Reyes", "cust": "Mario Bianchi",
                   "address_spoken": "120 Sydney Road, Coburg, Victoria, three zero five eight",
                   "dmo_crosstalk": True,
                   "dmo_ref_text": "The Default Market Offer is the [crosstalk] set by the [inaudible]. The "
                                   "estimated annual cost of this plan is four per cent [crosstalk] for an "
                                   "average household [inaudible]. This is an [inaudible] your [crosstalk].",
                   "dmo_asr": 0.58,
                   "email_spoken": "It's mario dot bianchi at bigpond dot com.",
                   "nmi_spoken": "6 1 0 2 6 6 5 5 4 4 3"},
        "crm": {"email": "mario.bianchi@bigpond.com", "nmi": "61026655443"},
        # The agent DID read it. An auditor listening closely would pass it; the machine
        # should route it to a human, never fail it on audio it could not hear.
        "truth": {},
    },
    {
        "lead_id": "3613811", "title": "Rate spoken in words, correct",
        "params": {"plan_id": "R1-SECURE-12", "agent": "Priya Kaur", "cust": "Chloe Martin",
                   "address_spoken": "2 Elm Avenue, Glen Iris, Victoria, three one four six",
                   "dmo_pct": "seven", "rate_spoken": "twenty-nine point seven",
                   "email_spoken": "It's chloe dot martin at outlook dot com.",
                   "nmi_spoken": "6 1 0 3 5 5 6 6 7 7 8"},
        "crm": {"email": "chloe.martin@outlook.com", "nmi": "61035566778"},
        "truth": {},
    },
    {
        "lead_id": "3613812", "title": "Two critical fails: wrong rate and wrong email",
        "params": {"plan_id": "R1-SAVER-FLEX", "agent": "Marcus Lee", "cust": "Ravi Menon",
                   "address_spoken": "77 Glenferrie Road, Hawthorn, Victoria, three one two two",
                   "rate_spoken": "twenty-nine point one",
                   "email_spoken": "It's ravi dot menon at gmail dot com.",
                   "nmi_spoken": "6 1 0 2 9 9 0 0 1 1 2"},
        "crm": {"email": "ravi.menon@gmial.com", "nmi": "61029900112"},
        "truth": {"rates_and_charges": "fail", "email_captured": "fail"},
    },
]

CRITICAL = {"recording_disclaimer", "account_holder", "dmo_verbatim", "rates_and_charges", "email_captured"}
SCORED = CRITICAL | {"nmi", "fuel_type"}


# ------------------------------------------------------------------------ output

def _duration(text: str, speaker: str) -> float:
    words = len(text.split())
    return round(max(1.2, words / (2.7 if speaker == AGENT else 2.4)), 1)


def to_transcript(sc: dict, script: list[dict], recorded_at: str) -> dict:
    """Reference transcript in the same shape as data/transcripts/*.json."""
    p = sc["params"]
    names = {AGENT: p["agent"].split()[0] + " " + p["agent"].split()[1][0] + ".",
             CUST: p["cust"].split()[0] + " " + p["cust"].split()[1][0] + "."}
    turns, clock = [], 0.0
    for i, line in enumerate(script):
        start = clock - line["overlap"] if line["overlap"] else clock + line["pause"]
        start = max(0.0, round(start, 1))
        end = round(start + _duration(line["text"], line["speaker"]), 1)
        text = line["ref_text"] or line["text"]
        turns.append({"idx": i, "speaker": line["speaker"], "speaker_name": names[line["speaker"]],
                      "text": text, "start_sec": start, "end_sec": end,
                      "asr_confidence": line["asr"] if line["asr"] is not None else 0.95})
        clock = max(clock, end)
    return {
        "lead_id": sc["lead_id"], "call_id": f"CALL-{sc['lead_id']}-01", "recorded_at": recorded_at,
        "duration_sec": round(clock + 1, 1),
        "audio_quality": "degraded" if p.get("dmo_crosstalk") else "clean",
        "asr_engine": "synthetic-reference (script timing estimate, no ASR)",
        "asr_version": "synth-1", "channels": "diarised",
        "turns": turns,
    }


def main() -> int:
    for sub in ("scripts", "transcripts", "audio"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)

    leads, truth = [], {}
    for n, sc in enumerate(SCENARIOS):
        p = sc["params"]
        lead_id = sc["lead_id"]
        recorded_at = f"2026-09-{10 + n % 8:02d}T{9 + n % 8:02d}:{(n * 7) % 60:02d}:00+10:00"
        script = build_call(p)

        (OUT / "scripts" / f"{lead_id}.json").write_text(json.dumps({
            "lead_id": lead_id, "title": sc["title"],
            "voices": {AGENT: p["agent"], CUST: p["cust"]},
            "lines": script,
        }, indent=2), encoding="utf-8")
        (OUT / "transcripts" / f"{lead_id}.json").write_text(
            json.dumps(to_transcript(sc, script, recorded_at), indent=2), encoding="utf-8")

        leads.append({
            "lead_id": lead_id, "retailer": "Retailer 1", "plan_id": p["plan_id"],
            "channel": "outbound_call", "agent_id": f"AG-{abs(hash(p['agent'])) % 900 + 1100}",
            "agent_name": p["agent"].split()[0] + " " + p["agent"].split()[1][0] + ".",
            "call_id": f"CALL-{lead_id}-01", "recorded_at": recorded_at,
            "audio_quality": "degraded" if p.get("dmo_crosstalk") else "clean",
            "transcript_path": f"data/synth/transcripts/{lead_id}.json",
            "audio_path": f"data/synth/audio/{lead_id}.wav",
            "synthetic": True,
            "crm": {
                "account_holder_name": p["cust"],
                "email": sc["crm"]["email"],
                "phone": f"04{n:02d} 555 {100 + n:03d}",
                "nmi": sc["crm"]["nmi"],
                "fuel_type": "electricity",
                "site_address": p["address_spoken"].split(",")[0] + ", VIC",
                "concession_card": False,
                "life_support": False,
            },
            "demo_note": f"SYNTHETIC - {sc['title']}",
        })
        checks = {cid: "pass" for cid in sorted(SCORED)}
        checks.update(sc["truth"])
        truth[lead_id] = {
            "title": sc["title"],
            "checks": checks,
            "gate": "HELD" if any(checks[c] == "fail" for c in CRITICAL) else "SUBMITTED",
            "labelled_by": "scenario author (hand label, independent of the scorer)",
        }

    # stable agent ids across runs (hash() is salted per process)
    ids = {}
    for lead in leads:
        ids.setdefault(lead["agent_name"], f"AG-{1300 + len(ids)}")
        lead["agent_id"] = ids[lead["agent_name"]]

    (OUT / "leads.json").write_text(json.dumps({
        "source": "Synthetic leads for development and evaluation - no real customer data",
        "generated_by": "tools/synth_calls.py",
        "leads": leads,
    }, indent=2), encoding="utf-8")
    (OUT / "truth.json").write_text(json.dumps({
        "note": "Hand-labelled expected outcome per check. 'pass' means an auditor listening to the "
                "call would pass it. The scorer never reads this file.",
        "leads": truth,
    }, indent=2), encoding="utf-8")

    chars = sum(len(l["text"]) for sc in SCENARIOS for l in build_call(sc["params"]))
    print(f"  wrote {len(SCENARIOS)} synthetic calls to {OUT.relative_to(ROOT)}")
    print(f"  TTS input: {chars:,} characters in total (ElevenLabs bills per character)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
