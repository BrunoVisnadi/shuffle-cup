import secrets

from django.db import migrations, models


def prepare_judges_and_allocations(apps, schema_editor):
    Judge = apps.get_model("tournament", "Judge")
    JudgeAllocation = apps.get_model("tournament", "JudgeAllocation")

    for judge in Judge.objects.all():
        judge.private_token = secrets.token_urlsafe()
        judge.save(update_fields=["private_token"])

    seen = set()
    allocations = JudgeAllocation.objects.order_by("round_id", "judge_id", "room__ordinal", "id")
    for allocation in allocations:
        key = (allocation.round_id, allocation.judge_id)
        if key in seen:
            allocation.delete()
        else:
            seen.add(key)


class Migration(migrations.Migration):
    dependencies = [("tournament", "0003_alter_breakchoice_choice_alter_round_kind")]

    operations = [
        migrations.AddField(
            model_name="judge",
            name="private_token",
            field=models.CharField(blank=True, default="", max_length=64),
            preserve_default=False,
        ),
        migrations.RunPython(prepare_judges_and_allocations, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="judge",
            name="private_token",
            field=models.CharField(default=secrets.token_urlsafe, max_length=64, unique=True),
        ),
        migrations.AlterUniqueTogether(name="judgeallocation", unique_together=set()),
        migrations.AddConstraint(
            model_name="judgeallocation",
            constraint=models.UniqueConstraint(fields=("round", "judge"), name="unique_judge_per_round"),
        ),
        migrations.AlterModelOptions(
            name="temporarypair",
            options={
                "ordering": [
                    "room__ordinal",
                    models.Case(
                        models.When(position="OG", then=0),
                        models.When(position="OO", then=1),
                        models.When(position="CG", then=2),
                        models.When(position="CO", then=3),
                        default=4,
                        output_field=models.IntegerField(),
                    ),
                    "id",
                ]
            },
        ),
        migrations.DeleteModel(name="BallotToken"),
    ]
