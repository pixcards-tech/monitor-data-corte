#!/usr/bin/env python3
"""
Descobrir servicoCodigo dos convênios travados em 243,
lendo a lista <servicos> devolvida por consultarMargem.
"""

import os
import sys
import time

import requests
from dotenv import load_dotenv
from xml.etree import ElementTree as ET

load_dotenv()

ENDPOINT = os.getenv("ZETRA_ENDPOINT", "https://api.econsig.com.br/central/services/HostaHostService")
CLIENTE  = os.getenv("ZETRA_CLIENTE", "PIX_CARD")
USUARIO  = os.getenv("ZETRA_USUARIO", "pix_card_xml")
SENHA    = os.getenv("ZETRA_SENHA", "")

if not SENHA:
    sys.exit("ZETRA_SENHA não configurada no .env — abortando.")

# convenio -> cpf de servidor daquele convenio
ALVOS = {
    "PIX_CARD-BELOHORIZONTE": "09080575631",
    "PIX_CARD-CURITIBA":      "12789393931",
    "PIX_CARD-POA":           "59015632049",
}

PAUSA = 1.5


def consultar_margem(convenio, cpf):
    soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tns="HostaHostService">
  <soap:Body>
    <tns:consultarMargem>
      <tns:cliente>{CLIENTE}</tns:cliente>
      <tns:convenio>{convenio}</tns:convenio>
      <tns:usuario>{USUARIO}</tns:usuario>
      <tns:senha>{SENHA}</tns:senha>
      <tns:cpf>{cpf}</tns:cpf>
      <tns:valorParcela>1.00</tns:valorParcela>
    </tns:consultarMargem>
  </soap:Body>
</soap:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": "urn:consultarMargem",
    }

    try:
        r = requests.post(ENDPOINT, data=soap.encode("utf-8"),
                          headers=headers, timeout=20, verify=True)
        if r.status_code != 200:
            return {"erro": f"HTTP {r.status_code}"}

        root = ET.fromstring(r.content)
        cod = root.find(".//{*}codRetorno")
        msg = root.find(".//{*}mensagem")

        servicos = []
        for s in root.findall(".//{*}servicos"):
            nome   = s.find(".//{*}servico")
            codigo = s.find(".//{*}servicoCodigo")
            servicos.append((
                codigo.text if codigo is not None else "?",
                nome.text if nome is not None else "?",
            ))

        return {
            "cod": cod.text if cod is not None else "?",
            "msg": msg.text if msg is not None else "",
            "servicos": servicos,
        }
    except Exception as e:
        return {"erro": f"{type(e).__name__}: {e}"}


def consultar_parametros(convenio, servico):
    soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tns="HostaHostService">
  <soap:Body>
    <tns:consultarParametros>
      <tns:cliente>{CLIENTE}</tns:cliente>
      <tns:convenio>{convenio}</tns:convenio>
      <tns:usuario>{USUARIO}</tns:usuario>
      <tns:senha>{SENHA}</tns:senha>
      <tns:servicoCodigo>{servico}</tns:servicoCodigo>
    </tns:consultarParametros>
  </soap:Body>
</soap:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": "urn:consultarParametros",
    }

    try:
        r = requests.post(ENDPOINT, data=soap.encode("utf-8"),
                          headers=headers, timeout=20, verify=True)
        root = ET.fromstring(r.content)
        sucesso = root.find(".//{*}sucesso")
        cod = root.find(".//{*}codRetorno")
        dia = root.find(".//{*}diaCorte")
        per = root.find(".//{*}periodoAtual")
        svc = root.find(".//{*}svcDescricao")

        if sucesso is not None and sucesso.text.lower() == "true":
            return {
                "ok": True,
                "dia": dia.text if dia is not None else "?",
                "periodo": per.text if per is not None else "?",
                "svc": svc.text if svc is not None else "?",
            }
        return {"ok": False, "cod": cod.text if cod is not None else "?"}
    except Exception as e:
        return {"ok": False, "cod": type(e).__name__}


print("=" * 95)
print("DESCOBRIR servicoCodigo VIA consultarMargem")
print("=" * 95)

resumo = []

for convenio, cpf in ALVOS.items():
    print(f"\n{convenio}")
    print("-" * 95)

    res = consultar_margem(convenio, cpf)
    time.sleep(PAUSA)

    if "erro" in res:
        print(f"  ✗ {res['erro']}")
        continue

    print(f"  codRetorno={res['cod']}  {res['msg'][:70]}")

    if not res["servicos"]:
        print("  ✗ resposta sem lista de serviços")
        continue

    print(f"\n  {len(res['servicos'])} serviço(s):")
    for codigo, nome in res["servicos"]:
        print(f"    {codigo:6}  {nome}")

    # testa consultarParametros com o primeiro serviço da lista
    primeiro = res["servicos"][0][0]
    print(f"\n  → testando consultarParametros com servicoCodigo={primeiro}")

    par = consultar_parametros(convenio, primeiro)
    time.sleep(PAUSA)

    if par["ok"]:
        print(f"    ✓ dia={par['dia']}  periodo={par['periodo']}  svc={par['svc']}")
        resumo.append((convenio, primeiro, par["dia"], par["periodo"]))
    else:
        print(f"    ✗ cod={par['cod']}")

print("\n" + "=" * 95)
print("RESUMO")
print("=" * 95 + "\n")

if resumo:
    for convenio, servico, dia, periodo in resumo:
        print(f"  ✓ {convenio:28} servicoCodigo={servico:6} dia={dia:3} periodo={periodo}")
else:
    print("  Nenhum destravado.")
print()