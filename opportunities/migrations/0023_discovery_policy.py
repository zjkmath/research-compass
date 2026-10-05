from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('opportunities', '0022_qs_audit_presence_semantics')]
    operations = [migrations.CreateModel(name='DiscoveryPolicy', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('key', models.SlugField(unique=True)), ('legacy', models.JSONField(default=dict)),
        ('registry_fingerprint', models.CharField(max_length=64)), ('activated_at', models.DateTimeField())])]
