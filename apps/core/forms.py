"""Formularios de la cuenta propia del usuario (Mi cuenta)."""
import re

from django import forms


class FormCambiarPin(forms.Form):
    """Cambio del PIN de firma propio (piso/mesa): la contraseña actual confirma
    la identidad; el PIN es de 4 a 6 dígitos y se captura dos veces."""

    password = forms.CharField(label="Tu contraseña", widget=forms.PasswordInput)
    pin = forms.CharField(label="PIN nuevo", widget=forms.PasswordInput(attrs={"inputmode": "numeric"}))
    pin2 = forms.CharField(label="Repite el PIN", widget=forms.PasswordInput(attrs={"inputmode": "numeric"}))

    def __init__(self, usuario, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario

    def clean_password(self):
        password = self.cleaned_data["password"]
        if not self.usuario.check_password(password):
            raise forms.ValidationError("La contraseña no es correcta.")
        return password

    def clean_pin(self):
        pin = self.cleaned_data["pin"].strip()
        if not re.fullmatch(r"\d{4,6}", pin):
            raise forms.ValidationError("El PIN es de 4 a 6 dígitos.")
        return pin

    def clean(self):
        datos = super().clean()
        if datos.get("pin") and datos.get("pin2") and datos["pin"] != datos["pin2"].strip():
            self.add_error("pin2", "Los PIN no coinciden.")
        return datos
