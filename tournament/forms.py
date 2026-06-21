from django import forms

from .models import BreakChoice, Judge


class CSVUploadForm(forms.Form):
    file = forms.FileField(help_text="UTF-8 CSV with the required header row.")


class BallotIdentityForm(forms.Form):
    submitted_by_name = forms.CharField(max_length=200)
    submitted_by_email = forms.EmailField(required=False)


class BreakChoiceForm(forms.ModelForm):
    class Meta:
        model = BreakChoice
        fields = ["choice"]


class JudgeAllocationForm(forms.Form):
    chair = forms.ModelChoiceField(queryset=Judge.objects.none(), required=False)
    panels = forms.ModelMultipleChoiceField(queryset=Judge.objects.none(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Judge.objects.filter(active=True)
        self.fields["chair"].queryset = queryset
        self.fields["panels"].queryset = queryset
