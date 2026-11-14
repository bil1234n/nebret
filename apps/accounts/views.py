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
        """``?full=1`` also returns the member's own pictures and which Equbs ask for this form."""
        full = request.query_params.get("full") in ("1", "true", "yes")
        qs = KycProfile.objects.filter(user=request.user)
        profile = (qs if full else qs.defer("photo", "id_photo")).first()
        data = {"profile": KycSerializer(profile).data if profile else None}
        if full:
            import base64

            def uri(blob):
                return f"data:image/jpeg;base64,{base64.b64encode(bytes(blob)).decode()}" if blob else None

            if profile:
                data["profile"].update(photo=uri(profile.photo), id_photo=uri(profile.id_photo),
                                       reviewed_at=profile.reviewed_at)
            data["equbs"] = _kyc_equbs(request.user)
        return Response(data)

    def post(self, request):
        d = request.data
        profile = services.submit_kyc(request.user, photo=request.FILES.get("photo"), national_id=d.get("national_id"),
                                      monthly_income=d.get("monthly_income"), id_photo=request.FILES.get("id_photo"))
        return Response({"profile": KycSerializer(profile).data}, status=status.HTTP_201_CREATED)


def _kyc_equbs(user) -> list[dict]:
    """The Equbs this member is in, and whether / when each one asks for the profile form."""
    from apps.equb import services as equb_services
    from apps.equb import standing as st
    from apps.equb.models import EqubMembership

    places = list(EqubMembership.objects.filter(user=user, status__in=EqubMembership.LIVE_STATUSES)
                  .select_related("group").order_by("-created_at"))
    standings = st.standings_for(places) if places else {}
    out = []
    for m in places:
        info = equb_services.kyc_info(m, standings[m.id].paid_rounds)
        out.append({"membership_id": m.id, "group": m.group.name, "slug": m.group.slug, "position": m.position,
                    "paid_rounds": standings[m.id].paid_rounds, **info})
    return out
