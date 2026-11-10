"""
Mapa de conceitos US GAAP -> colunas do universo (mesmos nomes da base brasileira sempre que o conceito é o mesmo).

Cada conceito é uma lista de ALTERNATIVAS, em ordem de prioridade.
Cada alternativa é uma lista de tags: a primeira é a âncora (precisa existir no período);
as demais são somadas se existirem no mesmo período.
Para cada exercício fiscal o coletor escolhe UMA alternativa (a de maior cobertura dentro do exercício;
empate -> prioridade), para que trimestres derivados (ex.: 4T = anual - 9M) não misturem tags diferentes.
"""

# ---------------------------------------------------------------- fluxos (duração)
DURACAO = {
    "receita": [["Revenues"], ["RevenueFromContractWithCustomerExcludingAssessedTax"],
                ["RevenueFromContractWithCustomerIncludingAssessedTax"], ["SalesRevenueNet"],
                ["SalesRevenueGoodsNet"], ["SalesRevenueServicesNet"], ["RevenuesNetOfInterestExpense"]],
    "cpv": [["CostOfRevenue"], ["CostOfGoodsAndServicesSold"], ["CostOfGoodsSold"], ["CostOfServices"],
            ["CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"]],
    "lucro_bruto": [["GrossProfit"]],
    "ebit": [["OperatingIncomeLoss"]],
    "lucro_liquido": [["ProfitLoss"], ["NetIncomeLoss"]],
    # ProfitLoss (consolidado) por último: algumas empresas só usaram essa tag em anos antigos (ex.: Agilent até 2019);
    # sem NetIncomeLoss no exercício, os não controladores costumam ser irrelevantes (a tag usada fica na auditoria)
    "lucro_controladores": [["NetIncomeLoss"], ["NetIncomeLossAvailableToCommonStockholdersBasic"], ["ProfitLoss"]],
    "da": [["DepreciationDepletionAndAmortization"], ["DepreciationAmortizationAndAccretionNet"],
           ["DepreciationAndAmortization"], ["DepreciationAmortizationAndOther"],
           ["Depreciation", "AmortizationOfIntangibleAssets"],
           ["DepreciationNonproduction", "AmortizationOfIntangibleAssets"]],
    "impairment": [["AssetImpairmentCharges"],
                   ["GoodwillImpairmentLoss", "ImpairmentOfIntangibleAssetsExcludingGoodwill",
                    "ImpairmentOfLongLivedAssetsHeldForUse"],
                   ["ImpairmentOfIntangibleAssetsExcludingGoodwill", "ImpairmentOfLongLivedAssetsHeldForUse"],
                   ["ImpairmentOfLongLivedAssetsHeldForUse"]],
    "receitas_financeiras": [["InvestmentIncomeInterest"], ["InterestIncomeOther"],
                             ["InvestmentIncomeInterestAndDividend"]],
    "despesas_financeiras": [["InterestExpense"], ["InterestExpenseNonoperating"], ["InterestExpenseDebt"],
                             ["InterestAndDebtExpense"]],
    "fco": [["NetCashProvidedByUsedInOperatingActivities"],
            ["NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]],
    "capex_imobilizado": [["PaymentsToAcquirePropertyPlantAndEquipment"], ["PaymentsToAcquireProductiveAssets"],
                          ["PaymentsForCapitalImprovements"]],
    "capex_intangivel": [["PaymentsToAcquireIntangibleAssets", "PaymentsToDevelopSoftware", "PaymentsForSoftware"],
                         ["PaymentsToDevelopSoftware", "PaymentsForSoftware"], ["PaymentsForSoftware"]],
    "vendas_imob_intang": [["ProceedsFromSaleOfPropertyPlantAndEquipment"], ["ProceedsFromSaleOfProductiveAssets"],
                           ["ProceedsFromSaleOfOtherPropertyPlantAndEquipment"]],
    "aquisicoes_participacoes": [["PaymentsToAcquireBusinessesNetOfCashAcquired"],
                                 ["PaymentsToAcquireBusinessesGross"],
                                 ["PaymentsToAcquireBusinessesAndInterestInAffiliates"]],
    "dividendos_pagos": [["PaymentsOfDividends"], ["PaymentsOfDividendsCommonStock"]],
    "recompra_acoes": [["PaymentsForRepurchaseOfCommonStock"], ["PaymentsForRepurchaseOfEquity"]],
    "emissao_acoes": [["ProceedsFromIssuanceOfCommonStock"]],
    "ir_csll_pagos": [["IncomeTaxesPaidNet"], ["IncomeTaxesPaid"]],          # nome mantido da base BR (= income taxes paid)
    "juros_pagos": [["InterestPaidNet"], ["InterestPaid"]],
    "principal_pago": [["RepaymentsOfDebt"],
                       ["RepaymentsOfLongTermDebt", "RepaymentsOfShortTermDebt", "RepaymentsOfLinesOfCredit"],
                       ["RepaymentsOfSeniorDebt", "RepaymentsOfNotesPayable", "RepaymentsOfConvertibleDebt",
                        "RepaymentsOfSecuredDebt", "RepaymentsOfUnsecuredDebt", "RepaymentsOfLinesOfCredit"],
                       ["RepaymentsOfLinesOfCredit"]],
    # arrendamentos (ASC 842): financeiro fica fora do EBITDA (como IFRS 16); operacional fica dentro do opex
    "pag_arrend_principal": [["FinanceLeasePrincipalPayments"], ["RepaymentsOfLongTermCapitalLeaseObligations"]],
    "pag_arrend_juros": [["FinanceLeaseInterestPaymentOnLiability"]],
    "pag_arrend_operacional": [["OperatingLeasePayments"]],
    "custo_arrend_operacional": [["OperatingLeaseCost"]],
}

# ---------------------------------------------------------------- saldos (instante)
INSTANTE = {
    "ativo_total": [["Assets"]],
    "ativo_circulante": [["AssetsCurrent"]],
    "caixa_equiv": [["CashAndCashEquivalentsAtCarryingValue"],
                    ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], ["Cash"]],
    "aplicacoes_cp": [["ShortTermInvestments"], ["MarketableSecuritiesCurrent"],
                      ["AvailableForSaleSecuritiesDebtSecuritiesCurrent"]],
    "passivo_total": [["Liabilities"]],
    "passivo_circulante": [["LiabilitiesCurrent"]],
    "passivo_nao_circulante": [["LiabilitiesNoncurrent"]],
    "passivo_e_pl": [["LiabilitiesAndStockholdersEquity"]],
    "patrimonio_liquido": [["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
                           ["StockholdersEquity"]],
    "pl_controladores": [["StockholdersEquity"]],
    "nao_controladores": [["MinorityInterest"]],
    "emprestimos_financiamentos": [["LongTermDebt", "ShortTermBorrowings", "CommercialPaper"],
                                   ["DebtCurrent", "LongTermDebtNoncurrent"],
                                   ["LongTermDebtNoncurrent", "LongTermDebtCurrent", "ShortTermBorrowings",
                                    "CommercialPaper"],
                                   ["LongTermDebtAndCapitalLeaseObligations",
                                    "LongTermDebtAndCapitalLeaseObligationsCurrent", "ShortTermBorrowings"],
                                   ["DebtAndCapitalLeaseObligations"]],
    "passivo_arrend_operacional": [["OperatingLeaseLiability"],
                                   ["OperatingLeaseLiabilityNoncurrent", "OperatingLeaseLiabilityCurrent"]],
    "passivo_arrend_financeiro": [["FinanceLeaseLiability"],
                                  ["FinanceLeaseLiabilityNoncurrent", "FinanceLeaseLiabilityCurrent"]],
    "clientes": [["AccountsReceivableNetCurrent"], ["ReceivablesNetCurrent"],
                 ["AccountsNotesAndLoansReceivableNetCurrent"]],
    "estoques": [["InventoryNet"]],
    "fornecedores": [["AccountsPayableCurrent"], ["AccountsPayableAndAccruedLiabilitiesCurrent"]],
}

# ações (unidade "shares")
ACOES_INSTANTE = {"acoes_balanco": [["CommonStockSharesOutstanding"]]}
ACOES_DURACAO = {"acoes_media_basica": [["WeightedAverageNumberOfSharesOutstandingBasic"]],
                 "acoes_media_diluida": [["WeightedAverageNumberOfDilutedSharesOutstanding"]]}
DEI_ACOES = "EntityCommonStockSharesOutstanding"


def todas_tags(mapa: dict) -> set:
    return {t for alts in mapa.values() for alt in alts for t in alt}


# ---------------------------------------------------------------- classificação setorial
# setor     = escritório da SEC (Division of Corporation Finance) responsável pela empresa ("ownerOrg")
# subsetor  = grupo SIC de 2 dígitos ("major group")
# segmento  = descrição SIC de 4 dígitos (como vem da SEC)
SETOR_SEC = {
    "Technology": "Tecnologia",
    "Trade & Services": "Comércio e Serviços",
    "Energy & Transportation": "Energia e Transporte",
    "Manufacturing": "Manufatura",
    "Finance": "Finanças",
    "Life Sciences": "Ciências da Vida",
    "Real Estate & Construction": "Imóveis e Construção",
    "Industrial Applications and Services": "Aplicações e Serviços Industriais",
    "Crypto Assets": "Criptoativos",
}

SIC_GRUPO = {
    "01": "Agricultural Production - Crops", "02": "Agricultural Production - Livestock",
    "07": "Agricultural Services", "08": "Forestry", "09": "Fishing, Hunting and Trapping",
    "10": "Metal Mining", "12": "Coal Mining", "13": "Oil and Gas Extraction",
    "14": "Mining of Nonmetallic Minerals", "15": "Building Construction - General Contractors",
    "16": "Heavy Construction", "17": "Construction - Special Trade Contractors",
    "20": "Food and Kindred Products", "21": "Tobacco Products", "22": "Textile Mill Products",
    "23": "Apparel and Finished Fabric Products", "24": "Lumber and Wood Products",
    "25": "Furniture and Fixtures", "26": "Paper and Allied Products",
    "27": "Printing and Publishing", "28": "Chemicals and Allied Products",
    "29": "Petroleum Refining", "30": "Rubber and Plastics Products", "31": "Leather Products",
    "32": "Stone, Clay, Glass and Concrete", "33": "Primary Metal Industries",
    "34": "Fabricated Metal Products", "35": "Industrial Machinery and Computer Equipment",
    "36": "Electronic and Electrical Equipment", "37": "Transportation Equipment",
    "38": "Instruments (Measuring, Medical, Optical)", "39": "Miscellaneous Manufacturing",
    "40": "Railroad Transportation", "41": "Local and Suburban Transit",
    "42": "Trucking and Warehousing", "43": "Postal Service", "44": "Water Transportation",
    "45": "Air Transportation", "46": "Pipelines (except Natural Gas)", "47": "Transportation Services",
    "48": "Communications", "49": "Electric, Gas and Sanitary Services",
    "50": "Wholesale - Durable Goods", "51": "Wholesale - Nondurable Goods",
    "52": "Building Materials and Garden Supply Retail", "53": "General Merchandise Stores",
    "54": "Food Stores", "55": "Auto Dealers and Gas Stations", "56": "Apparel and Accessory Stores",
    "57": "Home Furniture and Equipment Stores", "58": "Eating and Drinking Places",
    "59": "Miscellaneous Retail", "60": "Depository Institutions", "61": "Non-depository Credit Institutions",
    "62": "Security and Commodity Brokers", "63": "Insurance Carriers", "64": "Insurance Agents and Brokers",
    "65": "Real Estate", "67": "Holding and Other Investment Offices", "70": "Hotels and Lodging",
    "72": "Personal Services", "73": "Business Services", "75": "Auto Repair and Services",
    "76": "Miscellaneous Repair Services", "78": "Motion Pictures", "79": "Amusement and Recreation",
    "80": "Health Services", "81": "Legal Services", "82": "Educational Services", "83": "Social Services",
    "84": "Museums and Galleries", "86": "Membership Organizations",
    "87": "Engineering, Accounting, Research and Management", "89": "Services, NEC",
    "91": "Public Administration", "99": "Nonclassifiable",
}


def setor_de(owner_org: str) -> str:
    """'06 Technology' -> 'Tecnologia'; desconhecido -> texto original sem o número."""
    if not isinstance(owner_org, str) or not owner_org.strip():
        return "Não classificado"
    nome = owner_org.split(" ", 1)[1].strip() if owner_org[:2].isdigit() else owner_org.strip()
    return SETOR_SEC.get(nome, nome)


def subsetor_de(sic) -> str:
    s = str(sic or "").strip()
    if not s.isdigit():
        return "Não classificado"
    return SIC_GRUPO.get(s.zfill(4)[:2], f"SIC {s.zfill(4)[:2]}")


def tipo_especial(sic) -> str:
    """Marca empresas cujo modelo de análise difere (equivalente às 'financeiras' da base BR)."""
    s = str(sic or "").strip()
    if not s.isdigit():
        return ""
    n = int(s)
    if n == 6770:
        return "SPAC"
    if n == 6798:
        return "REIT"
    if 6000 <= n <= 6499 or n in (6199, 6211):
        return "financeira"
    return ""
