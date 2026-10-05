from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('opportunities', '0023_discovery_policy')]
    operations = [migrations.AddField(model_name='opportunity', name='identity_discriminator',
        field=models.CharField(max_length=180, blank=True))]
