from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .models import KycProfile, User, normalize_phone, validate_phone, validate_pin


class UserSerializer(serializers.ModelSerializer):
    referrals_count = serializers.IntegerField(source="referrals.count", read_only=True)
    company = serializers.SerializerMethodField()
    kyc_status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "phone", "email", "full_name", "preferred_language", "national_id",
            "date_of_birth", "city", "address", "referral_code",
            "referrals_count", "is_phone_verified", "date_joined", "company", "kyc_status",
        ]

    def get_company(self, user) -> dict | None:
        """Set for partner-company staff: the app opens the company workspace for them."""
        from apps.agencies.services import staff_for

        st = staff_for(user)
        if not st:
            return None
        a = st.agency
        return {"id": a.id, "name": a.name, "slug": a.slug, "kind": a.kind, "kind_label": a.get_kind_display(),
                "role": st.role, "role_label": st.get_role_display(), "can_manage": st.can_manage,
                "is_active": a.is_active, "logo_url": a.logo_url}

    def get_kyc_status(self, user) -> str | None:
        return KycProfile.objects.filter(user=user).values_list("status", flat=True).first()
        read_only_fields = ["phone", "referral_code", "is_phone_verified", "date_joined"]


class RegisterSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(max_length=20)  # normalised + validated in validate_phone
    password = serializers.CharField(write_only=True)
    referral_code = serializers.CharField(write_only=True, required=False, allow_blank=True)
    tokens = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["phone", "full_name", "email", "password", "preferred_language", "referral_code", "tokens"]

    def validate_phone(self, value):
        value = normalize_phone(value)
        validate_phone(value)
        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def validate_password(self, value):
        try:
            return validate_pin(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)

    def validate_referral_code(self, value):
        if not value:
            return None
        referrer = User.objects.filter(referral_code=value.strip().upper()).first()
        if not referrer:
            raise serializers.ValidationError("Invalid referral code.")
        return referrer

    def create(self, validated_data):
        from apps.luckydraw.services import on_referral_signup

        referrer = validated_data.pop("referral_code", None)
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, referred_by=referrer, **validated_data)
        if referrer:
            on_referral_signup(referrer=referrer, new_user=user)
        return user

    def get_tokens(self, user) -> dict:
        refresh = RefreshToken.for_user(user)
        return {"refresh": str(refresh), "access": str(refresh.access_token)}


class AccountDeletionPending(APIException):
    """Signing in to an account that asked to be deleted and is waiting for staff review."""

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = ("This account is suspended because you asked us to delete it. Our team is reviewing the request. "
                      "Contact Nebret support if you want to keep your account.")
    default_code = "deletion_pending"


class PhoneTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = User.USERNAME_FIELD

    def validate(self, attrs):
        attrs[self.username_field] = normalize_phone(attrs.get(self.username_field, ""))
        pending = User.objects.filter(phone=attrs[self.username_field],
                                      deletion_status=User.DeletionStatus.PENDING).first()
        if pending and pending.check_password(attrs.get("password", "")):
            raise AccountDeletionPending()
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class PushTokenSerializer(serializers.Serializer):
    push_token = serializers.CharField(max_length=255)


class DeleteAccountSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=5, max_length=1000, trim_whitespace=True,
                                   error_messages={"min_length": "Please tell us why in a few words (at least 5 letters).",
                                                   "blank": "Please tell us why you want to delete your account.",
                                                   "required": "Please tell us why you want to delete your account."})


class CheckPhoneSerializer(serializers.Serializer):
    phone = serializers.CharField(max_length=20)

    def validate_phone(self, value):
        value = normalize_phone(value)
        validate_phone(value)
        return value


class ChangePinSerializer(serializers.Serializer):
    current = serializers.CharField()
    new = serializers.CharField()

    def validate_new(self, value):
        try:
            return validate_pin(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)


class KycSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    has_id_photo = serializers.SerializerMethodField()

    class Meta:
        model = KycProfile
        fields = ["status", "status_label", "national_id", "monthly_income", "review_note", "submitted_at",
                  "has_id_photo"]

    def get_has_id_photo(self, obj) -> bool:
        return bool(obj.id_photo)
