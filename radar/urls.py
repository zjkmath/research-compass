from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from opportunities import views
from opportunities import priority20_views

urlpatterns = [
    path('priority20/', priority20_views.index, name='priority20'),
    path('priority20/<slug:key>/', priority20_views.detail, name='priority20_detail'),
    path('targets/<int:pk>/paths/<int:path_id>/support/', views.path_support, name='path_support'),
    path('profile/', views.profile, name='profile'),
    path('compare/', views.compare, name='compare'),
    path('targets/<int:pk>/budget/', views.budget, name='budget'),
    path('targets/<int:pk>/contact/', views.contact, name='contact'),
    path('decisions/<int:pk>/export/', views.export_decision, name='export_decision'),
    path('targets/', views.target_list, name='targets'),
    path('targets/<int:pk>/', views.target_detail, name='target_detail'),
    path('targets/<int:pk>/record/', views.save_target, name='target_record'),
    path('filters/save/', views.save_filter, name='save_filter'),
    path('filters/<int:pk>/', views.use_filter, name='use_filter'),
    path('updates/', views.update_review, name='update_review'),
    path('', views.opportunity_list, name='list'),
    path('opportunities/<int:pk>/', views.detail, name='detail'),
    path('opportunities/<int:pk>/record/', views.save_record, name='record'),
    path('workspace/', views.workspace, name='workspace'),
    path('sources/', views.sources, name='sources'),
    path('coverage/', views.coverage_registry, name='coverage'),
    path('sources/<int:pk>/recheck/', views.recheck_source, name='recheck_source'),
    path('directory/', views.directory, name='directory'),
    path('accounts/register/', views.register, name='register'),
    path('accounts/login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('admin/', admin.site.urls),
]
