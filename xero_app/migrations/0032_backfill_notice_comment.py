"""Point older notifications at the comment they were raised for.

DebtorNotice.comment arrived in 0031. Every notice raised before that stored
only a copy of the comment's text, so the Notifications page has no thread to
open for them and no comment to reply into - the row renders an expand arrow
that does nothing.

The link is recoverable: a notice carries the debtor and the exact text of the
comment that raised it, which together identify the comment. Where several
comments on a debtor share wording, the one written nearest the moment the
notice was raised is the one it came from.

Notices that match nothing keep a null comment and stay unexpandable, which is
correct - an allocation notice never had a comment, and a comment that has since
been deleted is genuinely gone.
"""
from django.db import migrations


def link_notices_to_comments(apps, schema_editor):
    DebtorNotice = apps.get_model("xero_app", "DebtorNotice")
    DebtorComment = apps.get_model("xero_app", "DebtorComment")

    notices = list(DebtorNotice.objects.filter(comment__isnull=True)
                   .exclude(kind="allocation"))
    if not notices:
        return

    # One pass over the comments on the debtors involved, keyed the same way the
    # notice text was written: comment.text truncated to the notice's field size.
    by_text = {}
    for c in DebtorComment.objects.filter(
            contact_id__in={n.contact_id for n in notices}):
        by_text.setdefault((c.contact_id, c.text[:2000]), []).append(c)

    matched = []
    for n in notices:
        candidates = by_text.get((n.contact_id, n.text))
        if not candidates:
            continue
        n.comment = min(candidates,
                        key=lambda c: abs((c.created_at - n.created_at).total_seconds()))
        matched.append(n)
    DebtorNotice.objects.bulk_update(matched, ["comment"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [("xero_app", "0031_debtornotice_comment")]

    operations = [
        migrations.RunPython(link_notices_to_comments, migrations.RunPython.noop),
    ]
