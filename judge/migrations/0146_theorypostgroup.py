# Generated by Django 3.2.19 on 2023-05-25 10:42

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0145_auto_20230525_0815'),
    ]

    operations = [
        migrations.CreateModel(
            name='TheoryPostGroup',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=20, unique=True, verbose_name='theory group ID')),
                ('full_name', models.CharField(max_length=100, verbose_name='theory group name')),
            ],
            options={
                'verbose_name': 'theory group',
                'verbose_name_plural': 'theory groups',
                'ordering': ['full_name'],
            },
        ),
    ]
