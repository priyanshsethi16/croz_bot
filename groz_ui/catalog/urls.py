from django.urls import path
from . import views

urlpatterns = [
    path('',          views.dashboard,    name='admin_dashboard'),
    path('login/',    views.admin_login,  name='admin_login'),
    path('logout/',   views.admin_logout, name='admin_logout'),
    path('api/upload/',   views.upload_pdf,    name='upload_pdf'),
    path('api/pdfs/',     views.list_pdfs,     name='list_pdfs'),
    path('api/pipeline/', views.run_pipeline,  name='run_pipeline'),
    path('api/ingest/',   views.run_ingest,    name='run_ingest'),
    path('api/chat/',     views.admin_chat,    name='admin_chat'),
    path('api/stats/',    views.catalog_stats, name='catalog_stats'),
    path('api/chunks/',       views.list_chunks,        name='list_chunks'),
    path('api/pdf-pages/',     views.pdf_page_count,     name='pdf_page_count'),
    path('api/split-pdf/',      views.split_pdf,          name='split_pdf'),
    path('api/split-pdf-custom/', views.split_pdf_custom,   name='split_pdf_custom'),
    path('api/pipeline-split/', views.run_pipeline_split, name='run_pipeline_split'),
    path('api/delete-pdf/',      views.delete_pdf,         name='delete_pdf'),
    path('api/api-keys/',         views.get_api_keys,       name='get_api_keys'),
    path('api/api-keys/save/',    views.save_api_keys,      name='save_api_keys'),
]
