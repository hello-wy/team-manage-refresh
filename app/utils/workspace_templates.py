"""Keep document and iframe representations distinct in HTTP caches."""
from fastapi.templating import Jinja2Templates


class WorkspaceTemplates(Jinja2Templates):
    def TemplateResponse(self, *args, **kwargs):
        response = super().TemplateResponse(*args, **kwargs)
        response.headers.add_vary_header("Sec-Fetch-Dest, X-Requested-With")
        return response
