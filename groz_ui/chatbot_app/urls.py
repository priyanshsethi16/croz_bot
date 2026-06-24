from django.urls import path
from . import views

urlpatterns = [
    path('',                 views.home,           name='home'),
    path('assistant/',       views.chat_home,      name='chat_home'),
    path('api/chat/',        views.chat_query,     name='chat_query'),
    path('api/categories/',  views.get_categories, name='get_categories'),
    path('logout/',          views.user_logout,    name='user_logout'),
]
