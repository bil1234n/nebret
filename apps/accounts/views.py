from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.core.exceptions import BusinessRuleError

from . import services
from .models import KycProfile, User
from .serializers import (ChangePinSerializer, CheckPhoneSerializer, DeleteAccountSerializer, KycSerializer,
                          PhoneTokenObtainPairSerializer, PushTokenSerializer, RegisterSerializer, UserSerializer)


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class LoginView(TokenObtainPairView):
    serializer_class = PhoneTokenObtainPairSerializer


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class PushTokenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        s = PushTokenSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        request.user.push_token = s.validated_data["push_token"]
        request.user.save(update_fields=["push_token"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeleteAccountView(APIView):
    """
    ``POST {"reason": "..."}`` – ask Nebret to delete this account.

    The account is suspended at once (sign-in stops working) and waits for staff approval in the console.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if request.user.is_staff:
            raise BusinessRuleError("Staff accounts are closed by an administrator.")
        s = DeleteAccountSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = services.request_deletion(request.user, s.validated_data["reason"])
        return Response({"status": user.deletion_status, "requested_at": user.deletion_requested_at},
                        status=status.HTTP_202_ACCEPTED)


class CheckPhoneView(APIView):
    """``POST {"phone"}`` → is there an account? The app then asks for the PIN, or opens "create account"."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        s = CheckPhoneSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        phone = s.validated_data["phone"]
        user = User.objects.filter(phone=phone).only("full_name", "is_active").first()
        return Response({"phone": phone, "registered": bool(user and user.has_usable_password()),
                         "first_name": user.first_name if user else ""})


class ChangePinView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        s = ChangePinSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        if not request.user.check_password(s.validated_data["current"]):
            raise BusinessRuleError("Your current PIN is not correct.")
        request.user.set_password(s.validated_data["new"])
        request.user.save(update_fields=["password"])
        return Response({"changed": True})


class KycView(APIView):
    """
    The member profile an Equb asks for before a member can win a draw.

    ``GET`` → ``{"profile": {...} | null}`` · ``POST`` multipart ``photo``, ``national_id``, ``monthly_income`` and
    optionally ``id_photo`` (a picture of the ID card).
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile = KycProfile.objects.filter(user=request.user).defer("photo", "id_photo").first()
        return Response({"profile": KycSerializer(profile).data if profile else None})

    def post(self, request):
        d = request.data
        profile = services.submit_kyc(request.user, photo=request.FILES.get("photo"), national_id=d.get("national_id"),
                                      monthly_income=d.get("monthly_income"), id_photo=request.FILES.get("id_photo"))
        return Response({"profile": KycSerializer(profile).data}, status=status.HTTP_201_CREATED)
