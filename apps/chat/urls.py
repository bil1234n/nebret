from django.urls import path

from . import views

urlpatterns = [
    path("conversations/", views.conversations),
    path("conversations/<int:pk>/", views.conversation),
    path("conversations/<int:pk>/messages/", views.messages),
    path("conversations/<int:pk>/messages/<int:message_id>/", views.message),
    path("conversations/<int:pk>/read/", views.read),
    path("conversations/<int:pk>/report/", views.report),
    path("conversations/<int:pk>/join/", views.join),
    path("unread/", views.unread),
    path("support/", views.support),
]
