#!/usr/bin/env python3
"""Build data/transcripts/3613793.json from the redacted CIMET artefact.

    python tools/build_3613793.py

Source: call-transcript-redacted.pdf ("Redacted Call Transcript - Outbound internet plan
sales call"). Every row below is copied verbatim, including the placeholder tags
([EMAIL], [PROVIDER_A], ...), transcription errors, disfluencies and run-together words.
Speaker 2 is the agent and Speaker 1 the customer, exactly as the source labels them -
including rows where the source has run both voices into one row.

The source has no audio and no timings. start_sec/end_sec are ESTIMATED from turn order
and word count (about 2.6 words a second, 0.5 s between turns), so the timestamps show
order and rough position, not exact moments. The source gives no ASR confidence;
0.9 is assumed for every turn and recorded as an assumption.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S1, S2 = "Speaker 1", "Speaker 2"

ROWS: list[tuple[str, str]] = [
    (S1, "Hello. [CUSTOMER_NAME] speaking."),
    (S2, "Yes. Hi, [CUSTOMER_NAME]. Good day. This is [AGENT_NAME] from Internet's comparison. How are you?"),
    (S1, "Good. Thanks. How are you?"),
    (S2, "Yeah. I'm good. Thank you. And we noticed that you're looking for better Internet plans, and we're calling to assist you with this.Yeah.K. And as I check it, your address is [SERVICE_ADDRESS] Correct. [UNCLEAR_NAME]. Yep. [SERVICE_ADDRESS]. K. By the way, please be advised that this call will be recorded for quality assuranceand, training purposes. K? Nowlet me double check the address. Bear with me.There you go. As I check it yeah. The address is already an NBN ready, fiber to the premises technology. Okay? Now just to ask, [CUSTOMER_NAME], who's your current Internet service provider? Do you have one?IPRIMUS.You are currently with iPRIMUS.How much are you paying?"),
    (S1, "They reduced it today to sixty five. That's all right here."),
    (S2, "Sixty five dollars for how many MBPS? Do you know the speed? How do you care? Twenty five.Twenty five Mbps?Yeah.Okay.And how many people are using the Internet?Just myself.Are you used to Internet? Are you do gaming,video streaming?"),
    (S1, "Work from home? Yeah. I do. No. I don't work from home. I just watch, you know, Netflix and that sort of stuff, andI've seen my,TV still at, like, channel seven channel nine, whatever. Okay. I stream that through the Internet."),
    (S2, "And do you need the home phone line?Do you need a landline or no?"),
    (S1, "No."),
    (S2, "Okay. Because I just want to tell you, I can give you a twenty five MBBS for,only forty two dollars and ninetyMhmm. For the first six months."),
    (S1, "Yep. And then what does it go up to?"),
    (S2, "Seventy two dollars and ninety. That's the regular price. Yeah. See, I'm I'm beyond that. But with that, I"),
    (S1, "might save myself twenty dollars a month for six months, butthen it's then I have seven dollars a month here."),
    (S2, "I'm going on. But So you're notno last in contract with the plan. It's just a month to month contract.I mean Yeah. Before the six weeks expire,you can visit again our website and see if we can give you another set of promotion of this, ma'am."),
    (S1, "Yeah. Yeah. Maybe I'll juststay where I am. I couldn't be bothered because I have to paynot much savings."),
    (S2, "I really understand. Again, that's still Okay.Twenty dollars.I can also give you a free modem with thatwith no extra cost."),
    (S1, "Who is that through?"),
    (S2, "That is from [PROVIDER_A]."),
    (S1, "Right. [PROVIDER_A].So free modemYep. And forty nine dollars a month for forty two dollars a month. Dollars.Yeah.Do I get a check for it?"),
    (S2, "No. All the NBN,all all the NBN plans, they're using only one network, all the retailers. That's the NBN phone.K? But for thethe mobile SIM plan, kindly use it at Telstra network."),
    (S1, "I can.And would it be a new modemor a refurbished one?"),
    (S2, "So that's the brand new modembrand new modem. It's all yours.It's hundred percent free. Even you switch provider,even you move to a different property,you can keep and use the same modem."),
    (S1, "No need to repair. Happens if what happens if at the end of six months when it goes up to seventy two dollarsand I change,do I get to get the modemor do I get to the account then? No. No."),
    (S2, "You it's hundred percent free. It's all yours. Even you switch provider,even you move to a different property,you can use and keep it. No need to return it. K? Again, it's hundred percent free."),
    (S1, "Okay. So how does it how does it work? How does how doesget changed over?"),
    (S2, "No. We can quickly set this up for you without paying any status fee.K? NowI just want to tell you that how do I get the modem?Yeah. It will be delivered to you within three to five business days."),
    (S1, "Okay. And is there a cost involved in having it delivered?"),
    (S2, "No. It's hundred percent. No extra cost."),
    (S1, "Okay.Alright. Yep. Sounds like a good deal."),
    (S2, "Yeah. That's why we can quickly set it up for you so you can get the free modem,k, within three to five business days. Now I just want to tell you that again, here are some detail, here here are some more details about it, ma'am. This value plan from [PROVIDER_A]helps with comes with a one to one contract only and, again, provides twenty five Mbps typical in download speed and eight point five Mbps typical in the upload speed from seven PM to eleven PM. Again, the original plan cost is seventy two dollars and ninety per month, but we have an offer ongoing where you will get this plan as forty two dollars and ninety only per month for the first six months and then seventy two dollars and ninety. K? And then,I just want to tell you that the modem that you will receive is the Netcom CF forty Wi Fi six modem. K? Again, it's hundred percent free. No extra cost. It's all yours. K? And then the good thing with the modem, [CUSTOMER_NAME], it's suitable for FTTT,HFC,FTTs, and fixed wireless connection types. That means it's almost compatible to all NBN plans. K? And this is already a Wi Fi six modem.It's already preconfigured. That means it's plugged and free. K? And it's still used in the twenty ten. Okay? So the delivery of the modem into a PowerPointwhen I get it? You just did And it's good to go. K?"),
    (S1, "Okay. So there's no setting up or anything like that. You just plug it in? Yep."),
    (S2, "Because it's already preconfigured to [PROVIDER_A]. K? That's why you you don't need to reconfigure it. K? Okay. Yep.And then again, the total minimum cost will be forty two dollars and ninety only. No setup fee. No any additional cost. K?Yep.And this will be under your name. Am I correct?Yes.How do you want to address your name, dismissed or missus, or do you have any title?"),
    (S1, "Missus [CUSTOMER_NAME].Yeah. [CUSTOMER_FULL_NAME]."),
    (S2, "Yeah. Can you please verify again your first and last name as per ID, please?"),
    (S1, "Sorry. I didn't get that."),
    (S2, "Can you please verify your first and last name as per ID?"),
    (S1, "[CUSTOMER_FULL_NAME]."),
    (S2, "[CUSTOMER_NAME] or [CUSTOMER_NAME]?"),
    (S1, "Well, I go by [CUSTOMER_NAME]."),
    (S2, "[CUSTOMER_NAME]. Okay. [CUSTOMER_NAME]. Yeah.Okay. But, again,[CUSTOMER_NAME]. Right? Because that is the exact bill that you will,you will see on the bill.K? Yep. Mhmm. Missus [CUSTOMER_FULL_NAME]. Right?Yes.Okay. And then your email address, can you please also verify it? It's [EMAIL].Thank you. And then your mobile number, can you please also verify it?"),
    (S1, "[PHONE]."),
    (S2, "Okay. And your date of birth?"),
    (S1, "[DOB]."),
    (S2, "Okay. And then do you want to add a secondary mobile number?"),
    (S1, "Sorry. What was that?"),
    (S2, "Do you want to add a secondary mobile number or alternate mobile number?No. Okay.And you are currently with I Primus. Right?"),
    (S1, "That's correct. Yes."),
    (S2, "Are you able to pull up your I Primus bill or no? Right now? Hang yep. Hang on.Yes, please."),
    (S1, "Yep. Got [ACCOUNT_NUMBER]."),
    (S2, "Can you please check if you can see the a b c ID?A b c ID."),
    (S1, "A b c?"),
    (S2, "Yep. A b c I d."),
    (S1, "I got issue date. I've seen that balance.Clear button.Customer number? Is that it?"),
    (S2, "No. ABC IDs.You can,you can check it, but if it's not visible, it's okay."),
    (S1, "I've got Internet service number."),
    (S2, "It's okay if you don't see it. That means it's not visible on the bill."),
    (S1, "Yeah. No. I I can't see it anywhere there. It's okay. And then again,"),
    (S2, "the connection address will be [SERVICE_ADDRESS]. Correct? Yeah. And how how soon do you want your connection to be at the address? As soon as possible, or do you prefer the same? As soon as possible.As soon as possible. Okay. And, do you want your modem to be delivered at the same address, or do you want a different address?Same address? Same address. Yeah. Okay. Again, the delivery will be three to five business days. K?"),
    (S1, "Yeah.No. Just mhmm. Just with with the delivery,of the modem, the [STREET_NAME]entrance is closed at the moment. Sothey will have to come to [DELIVERY_ADDRESS].There's two entrances.[DELIVERY_ADDRESS]."),
    (S2, "I see. Okay."),
    (S1, "Yeah. Because I can't get you in the pre code."),
    (S2, "I see. Okay.It's already noted. Okay? Now, again, to set up your account, [CUSTOMER_NAME], we need to collect your preferred payment method. Are you using a credit card or debit card?"),
    (S1, "And my what, sir?"),
    (S2, "I again, to set up your account, we need to collect your preferred payment method because this will be direct debitedevery month from the account from the account. Mhmm."),
    (S1, "Okay."),
    (S2, "Are youokay. But before that, I need to mute the recording. K?Okay. The recording is already resumed. Now mhmm.Can you please,do you have access on your email right now? Right?"),
    (S1, "Yes. I do."),
    (S2, "Okay. I'm not sending in.Okay. Can you please check? I just sent it, I think ten minutes ago, the email.Just let me know if you repeat. It's from Equinix comparison."),
    (S1, "Yep.Yep. Correct. Yep."),
    (S2, "Yeah. Open the email and then click view plan."),
    (S1, "Yeah."),
    (S2, "Okay. After you click view plan,you will see the plan details. Right?"),
    (S1, "Yep. So you're in the Yep. Travel there, and travel."),
    (S2, "So on the lower part of it, can you please click apply now?Yes.Okay. After you click apply now yep. Click apply now, and it will load,and you will go to the modem.K? Now on the modem,can you please look for the Netcom p s forty Wi Fi six modem?"),
    (S1, "The Wi Fi six pre configured. Is that the one?"),
    (S2, "Yeah. The Netcom CF forty Wi Fi six. Right? CF forty Wi Fi six.Yep. Do you see the zero dollar upfront? Right? Yep. Yep. Yep. Yeah. Can you please select it? Make sure that you selected it."),
    (S1, "Yep. I've selected that."),
    (S2, "Okay. And then after that, scroll it down.Yep.Now you will see the total minimum cost of three hundred seventeen dollars. Right?"),
    (S1, "Yeah."),
    (S2, "Yep. You don't have to worry. If you will see the new development fee, right, of two hundred seventy five dollars,You don't have to worry.You will not pay for that because your address is already an NBN ready. That is only applicablefor a newly built house. K? Because they need to install the NBN infrastructure of the address. That's why if you can see, the June development fee, there's aopen and close parenthesisifapplicable.K?But your address is not applicable for that. K? So you just need the payment. And then click next personal detail."),
    (S1, "Yep. Next address details?"),
    (S2, "Yeah. Make sure all the information are correct, and then click next address detail.It's still showing that minimum cost of three hundred and seventeen, but don't worry about that. I understand. It's okay. You don't need to worry. Okay? It will not be charged the two hundred seven dollars.And then,next address, you will see the address.Yeah. You will see, next address yep. You will need to, select as soon as possibleon the connection date.Andthenon the delivery of the modem, selectyes. K?I mean, the the the, thethe address of the delivery."),
    (S1, "Okay. So do you want the modem to be delivered at the current address? Click no? Yes. Select yes. No. Select yes. Because it will be the same address. Right? It will be delivered at the same address. No. They've got a delivery to [DELIVERY_ADDRESS]."),
    (S2, "I see.Okay. Can you please, select no?Yep. Click no and then select the exact address. K? Yeah."),
    (S1, "[DELIVERY_ADDRESS].Opt"),
    (S2, "Just let me know if you have any additional questions. K?"),
    (S1, "It says addressaddress not found. Please enter correct addressor enter it manually. So I've entered it manually. Right. [UNCLEAR_NAME], can you please enter it manually?"),
    (S2, "That"),
    (S1, "I'm trying to stop not still. I'm trying to k.It's still coming up with that. I'll put my card details in."),
    (S2, "So it's still coming up with that three hundred and seventeen dollars, but don't worry about that. I see. You don't need to worry. I really guarantee you it will not be charged. K? Okay. So You need to only forty forty two dollars. Detail?Yep. Click next review details. Make sure that the, you,tick the boxesthat you agree. Yep."),
    (S1, "Mhmm.Yep."),
    (S2, "Nah. Just me. And you will seemhmm."),
    (S1, "I'll just try putting it in again."),
    (S2, "Again, what is the delivery address? Can you please tell me?"),
    (S1, "It's [DELIVERY_ADDRESS]."),
    (S2, "Okay.Okay. Can do it. Yeah. It's okay. Can you please select,can you please selectyes. I will be the one who will change the delivery address. K? So you will not be worried. Again,[SERVICE_ADDRESS]. Right?[SERVICE_ADDRESS]? No. No."),
    (S1, "No. The address for [STREET_NAME] is [SERVICE_ADDRESS]."),
    (S2, "Okay.Hang on. That might let me delivery address?"),
    (S1, "Hang on. Let me [DELIVERY_ADDRESS].Yeah. It's because it's a,[RESIDENTIAL_COMPLEX], and there's two entrances.And one entrance the [STREET_NAME] entrance is closed at the moment,so you can't get up that road."),
    (S2, "I see. Because"),
    (S1, "It's okay. It's I think it's allowing me to put it inbit by bit."),
    (S2, "Okay. [SERVICE_ADDRESS]."),
    (S1, "[SERVICE_ADDRESS]"),
    (S2, "."),
    (S1, "Okay. So a text message, [OTP_CODE]."),
    (S2, "Mhmm. And then click submit application.Just let me know if you already have the reference number."),
    (S1, "Yep."),
    (S2, "What is the reference number?"),
    (S1, "[REFERENCE_NUMBER]."),
    (S2, "Thank you for that. That means that you already take advantage of the offer. Okay?"),
    (S1, "Yeah.So this is an Ambien. Yeah."),
    (S2, "That is an Ambien. K? Yeah. Yeah. Yeah.Okay. Congratulationsfor choosing [PROVIDER_A]. You need to wait for the delivery of the modem. It will be three to five business days. K? And after that, you can activate and connect it. And then now since we already help you with your Internet, how about your electricity and gas? Maybe we could also give you a picture of that. No. I don't have gas, and my electricity"),
    (S1, "is all,With aircon? I buy it off the complex that I live in. It doesn't go through I see. So it's embedded. Or anything like that. Yeah."),
    (S2, "I see. Okay. If that's the case here, again, [CUSTOMER_NAME], congratulations for choosing [PROVIDER_A].Thank you also for choosing Econnex Comparison. Do you have another question?"),
    (S1, "No. Do you have acontact phone number I can contact you on if"),
    (S2, "I have You will see it,on the, yeah. You will see it on the,you will see the page of the reference number. Right? Below that, you will see all the contact number that you can contact. K?"),
    (S1, "Yeah. Okay. Yep."),
    (S2, "Yep. This is it?Okay. Thank you. From the lower price. Okay. Yeah. Again, thank you also, [CUSTOMER_NAME]. Okay? Congratulations.This is [AGENT_NAME] again from Equinix Comparison. Again, it was a pleasure to help you out. K? Cheers, and have a wonderful day."),
    (S1, "Okay. Thank you."),
    (S2, "You're welcome.Bye for now. Bye."),
]

ROLE = {S2: "agent", S1: "customer"}
WORDS_PER_SEC, GAP_SEC, ASSUMED_ASR = 2.6, 0.5, 0.9


def main() -> int:
    turns, clock = [], 0.0
    for i, (speaker, text) in enumerate(ROWS):
        start = round(clock + (GAP_SEC if i else 0.0), 1)
        end = round(start + max(1.0, len(text.split()) / WORDS_PER_SEC), 1)
        turns.append({"idx": i, "speaker": ROLE[speaker], "speaker_name": speaker, "text": text,
                      "start_sec": start, "end_sec": end, "asr_confidence": ASSUMED_ASR})
        clock = end
    doc = {
        "lead_id": "3613793",
        "call_id": "CALL-3613793-01",
        "recorded_at": None,
        "duration_sec": turns[-1]["end_sec"],
        "audio_quality": "unknown",
        "asr_engine": "source transcript (redacted PDF, provider unknown)",
        "asr_version": None,
        "channels": "diarised (Speaker 2 = agent, Speaker 1 = customer, as labelled in the source)",
        "source_document": "call-transcript-redacted.pdf - Redacted Call Transcript, outbound internet plan sales call",
        "redaction": "de-identified at source with placeholder tags; tags are kept verbatim and never stripped",
        "timing_estimated": True,
        "timing_note": f"No audio or timings in the source. start_sec is estimated from turn order at "
                       f"{WORDS_PER_SEC} words/s with {GAP_SEC}s between turns. Timestamps show order, not exact moments.",
        "asr_confidence_note": f"The source gives no ASR confidence; {ASSUMED_ASR} is assumed for every turn.",
        "turns": turns,
    }
    out = ROOT / "data" / "transcripts" / "3613793.json"
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)}: {len(turns)} turns, ~{turns[-1]['end_sec'] / 60:.1f} min (estimated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
