from django import forms

from .models import BreakChoice, Judge


class CSVUploadForm(forms.Form):
    file = forms.FileField(label="Arquivo", help_text="CSV em UTF-8 com a linha de cabeçalhos obrigatórios.")


class BallotIdentityForm(forms.Form):
    submitted_by_name = forms.CharField(max_length=200)
    submitted_by_email = forms.EmailField(required=False)


class BreakChoiceForm(forms.ModelForm):
    class Meta:
        model = BreakChoice
        fields = ["choice"]


class JudgeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, judge):
        return f"{judge.name} - {judge.society or 'Sem sociedade'}"


class JudgeMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, judge):
        return f"{judge.name} - {judge.society or 'Sem sociedade'}"


class JudgeAllocationForm(forms.Form):
    chair = JudgeChoiceField(queryset=Judge.objects.none(), required=False, label="Chair")
    panels = JudgeMultipleChoiceField(
        queryset=Judge.objects.none(), required=False, label="Panel",
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Judge.objects.filter(active=True).select_related("society")
        self.fields["chair"].queryset = queryset
        self.fields["panels"].queryset = queryset
