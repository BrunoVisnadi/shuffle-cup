from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("tournament", "0001_initial")]

    operations = [
        migrations.RemoveField(model_name="debaterpartnerconflict", name="reason"),
        migrations.RemoveField(model_name="judgedebaterconflict", name="reason"),
    ]
