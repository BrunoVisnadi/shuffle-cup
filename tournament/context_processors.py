from .models import SiteSettings


def public_settings(request):
    return {"public_setting": SiteSettings.load()}
