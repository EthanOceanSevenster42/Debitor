"""LBINC // FSA legal collections workflow definition.

Static catalogue of the steps an attorney works through once a company is handed
over to the lawyers. Mirrors the LBINC workflow document:

    Collections (linear) -> choose a route:
        * Summons                 -> Unopposed | Opposed (Defended)
        * Application for payment  -> Unopposed | Opposed

Each step has a stable key (stored against LegalStep / LegalStepComment), a label,
and an ``is_variation`` flag for the comment-only "Variations" step at the end of
each branch. Attorneys tick steps done and may comment on any step; they can also
switch a matter between Unopposed and Opposed if the debtor changes strategy.
"""

TRACK_SUMMONS = "summons"
TRACK_APPLICATION = "application"
TRACKS = [(TRACK_SUMMONS, "Summons"), (TRACK_APPLICATION, "Application for payment")]
TRACK_LABELS = dict(TRACKS)

# Each entry: (key, label, is_variation)
COLLECTIONS = [
    ("receipt_instruction", "Receipt of Instruction", False),
    ("draft_lod", "Draft LOD (Letter of Demand)", False),
    ("lod_email", "Service of LOD via Email", False),
    ("lod_sheriff", "Service of LOD via Sheriff", False),
    ("default_listing_28", "After 28 days — proceed with default listing with credit bureaus (if no bona fide reason is provided to dispute payment)", False),
    ("debtor_listed", "Debtor listed as a default payer", False),
    ("issue_summons_or_application", "Issue summons where quantum justifies, alternatively launch application for payment (if no disputed facts)", False),
]

SUMMONS_UNOPPOSED = [
    ("su_summons_issued", "Summons Issued", False),
    ("su_default_judgement", "Apply for default Judgement", False),
    ("su_court_order", "Court Order granted", False),
    ("su_tax_bill", "Tax Bill of Costs", False),
    ("su_warrant", "Proceed with warrant of execution", False),
    ("su_variations", "Variations", True),
]

SUMMONS_OPPOSED = [
    ("so_summons_issued", "Summons Issued", False),
    ("so_notice_defend", "Notice to Defend received (Served and Filed)", False),
    ("so_plea", "Defendant's plea received (Served and Filed)", False),
    ("so_summary_judgement", "Apply for Summary Judgement (if Defendant did not set out a bona fide defence good in law in its plea)", False),
    ("so_replication", "Plaintiff's replication served and filed (if applicable)", False),
    ("so_discovery", "Discovery phase", False),
    ("so_pretrial", "Pre-trial phase", False),
    ("so_trial_date_app", "Application for a trial date", False),
    ("so_trial_date_alloc", "Trial date allocated", False),
    ("so_trial", "Trial", False),
    ("so_order", "Order granted", False),
    ("so_tax_bill", "Tax Bill of Costs", False),
    ("so_warrant", "Proceed with warrant of execution", False),
    ("so_variations", "Variations", True),
]

APPLICATION_UNOPPOSED = [
    ("au_service_sheriff", "Service via Sheriff", False),
    ("au_hearing_date", "Apply for an unopposed hearing date", False),
    ("au_brief_counsel", "Brief counsel (if required)", False),
    ("au_attend_hearing", "Attend to hearing", False),
    ("au_court_order", "Court Order for payment obtained", False),
    ("au_tax_bill", "Tax Bill of Costs", False),
    ("au_warrant", "Attend to warrant of execution (if required)", False),
    ("au_variations", "Variations", True),
]

APPLICATION_OPPOSED = [
    ("ao_notice_oppose", "Notice of Intention to oppose served and filed", False),
    ("ao_answering", "Respondent's answering affidavit served and filed", False),
    ("ao_replying", "Replying affidavit to Respondent's answering affidavit served and filed (if required)", False),
    ("ao_brief_counsel", "Brief counsel to attend hearing (if required)", False),
    ("ao_court_order", "Court Order for payment obtained", False),
    ("ao_tax_bill", "Tax Bill of Costs", False),
    ("ao_warrant", "Proceed with warrant of execution (if required)", False),
    ("ao_variations", "Variations", True),
]


def _step(t):
    return {"key": t[0], "label": t[1], "is_variation": t[2]}


def sections_for(summons_opposed, application_opposed):
    """The matter's full, sequential workflow:

        Collections  ->  Summons (Unopposed|Opposed)  ->  Application for payment
        (Unopposed|Opposed)

    All three sections always show, in order. Each litigation section carries its
    own Unopposed/Opposed state and swaps its steps accordingly. Each section is
    {key, title, which, opposed, steps:[{key,label,is_variation}]} where ``which``
    is the toggle target ("summons"/"application"), empty for Collections."""
    return [
        {"key": "collections", "title": "Collections", "which": "", "opposed": False,
         "steps": [_step(t) for t in COLLECTIONS]},
        {"key": "summons", "title": "Summons", "which": "summons", "opposed": bool(summons_opposed),
         "steps": [_step(t) for t in (SUMMONS_OPPOSED if summons_opposed else SUMMONS_UNOPPOSED)]},
        {"key": "application", "title": "Application for payment", "which": "application",
         "opposed": bool(application_opposed),
         "steps": [_step(t) for t in (APPLICATION_OPPOSED if application_opposed else APPLICATION_UNOPPOSED)]},
    ]


def visible_step_keys(summons_opposed, application_opposed, include_variations=False):
    """Step keys currently shown for the matter (for progress counting)."""
    return [s["key"] for sec in sections_for(summons_opposed, application_opposed)
            for s in sec["steps"] if include_variations or not s["is_variation"]]


# Every key that can legitimately be stored, for validation on POST.
ALL_STEP_KEYS = {
    t[0] for group in (COLLECTIONS, SUMMONS_UNOPPOSED, SUMMONS_OPPOSED,
                       APPLICATION_UNOPPOSED, APPLICATION_OPPOSED)
    for t in group
}

# Stable step key -> human label, used by the milestone timeline. Because the
# Summons/Application branches each have an Unopposed and an Opposed variant with
# the same label, this de-duplicates by key. `_route` is the pseudo-key under
# which route / Unopposed↔Opposed changes are logged.
STEP_LABELS = {
    t[0]: t[1]
    for group in (COLLECTIONS, SUMMONS_UNOPPOSED, SUMMONS_OPPOSED,
                  APPLICATION_UNOPPOSED, APPLICATION_OPPOSED)
    for t in group
}
STEP_LABELS["_route"] = "Route / defence"


def step_section(step_key):
    """Which section a step key belongs to ('Collections' / 'Summons' /
    'Application for payment'), for labelling timeline entries."""
    if step_key.startswith(("su_", "so_")):
        return "Summons"
    if step_key.startswith(("au_", "ao_")):
        return "Application for payment"
    if step_key == "_route":
        return ""
    return "Collections"


def _step_events(matter):
    """Completed steps plus lifecycle milestones for a matter, oldest first.

    Reads ``matter.step_states`` with ``.all()`` so a prefetched queryset costs no
    extra query. Comments and route changes are deliberately absent: this feeds
    "last action", which means work the attorneys actually ticked off.
    """
    events = []

    def add(dt, who, kind, title, section=""):
        if dt:
            events.append({"date": dt, "who": who or "-", "kind": kind,
                           "title": title, "section": section})

    add(matter.sent_at, matter.sent_by, "milestone", "Sent to the lawyers")
    add(matter.approved_at, matter.approved_by, "milestone", "Approved - active with lawyers")
    for s in matter.step_states.all():
        if not s.done:
            continue
        # Completed steps normally carry done_at; fall back so a step never
        # silently vanishes if the timestamp is missing.
        add(s.done_at or matter.approved_at or matter.sent_at, s.done_by, "step",
            STEP_LABELS.get(s.step_key, s.step_key), step_section(s.step_key))
    if matter.status == matter.CLOSED:
        add(matter.closed_at, matter.closed_by, "milestone", "Closed / brought back from lawyers")

    events.sort(key=lambda e: e["date"])
    return events


def last_action(matter, timeline=None):
    """What the lawyers last actually *did* on a matter.

    The newest ticked workflow step; before any step is done, the latest lifecycle
    milestone, so the answer always says where the matter stands. Pass ``timeline``
    when the caller has already built the fuller history (comments and route
    changes included) to reuse it instead of re-reading the steps — the selection
    rule stays here either way, so the Lawyers card and the weekly report agree.

    Returns ``{"date", "who", "kind", "title", "section"}`` or None.
    """
    events = _step_events(matter) if timeline is None else timeline
    steps = [e for e in events if e["kind"] == "step"]
    if steps:
        return steps[-1]
    return next((e for e in reversed(events) if e["kind"] == "milestone"), None)
