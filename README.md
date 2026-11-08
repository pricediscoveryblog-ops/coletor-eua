# coletor-eua — universo americano (Price Discovery)

Réplica do `universo-setorial` para empresas listadas nos EUA (NYSE, Nasdaq, Cboe, TXSE), com dados da SEC.

## Fonte de dados

| Dado | Fonte | Observação |
|---|---|---|
| Demonstrações (anual/trimestral) | SEC XBRL `companyfacts` (bulk `companyfacts.zip` no domingo; API por empresa no incremental) | Gratuito, sem chave |
| Universo e tickers | `company_tickers_exchange.json` | Só NYSE/Nasdaq/Cboe/TXSE; OTC excluído |
| Setor, SIC, ano fiscal | `submissions` por empresa (cache de 120 dias em `cache/`) | |
| Regras de acesso | User-Agent declarado; 5 req/s (limite da SEC: 10) | |

## Agendamento

Carga completa aos domingos às 04h de Brasília. Incremental de terça a sábado às 08h, cobrindo quem publicou 10-K/10-Q nos últimos 5 dias. Também pode ser disparado manualmente.

Os dados ficam na branch `dados`, com **um único commit**, sobrescrito a cada execução, para não inflar o repositório. O código fica na `main`.

## Saídas (`dados/` na branch `dados`)

Os arquivos são `eua_anual.parquet`, `eua_trimestral.parquet` e `eua_ltm.parquet`. Eles usam os mesmos nomes de coluna da base BR sempre que o conceito é o mesmo. A coluna `cnpj` guarda `CIK##########` para manter compatibilidade com o app.

Os demais arquivos são:
- `acoes_eua.parquet`: ações em circulação, com fonte e `alerta_salto`.
- `cadastro_eua.parquet` e `auditoria_eua.csv.gz`.
- `metadados.json` e `sonda_precos.json`.

Unidades: US$ milhões; ações em milhões.

## Classificação

| Nível | Origem |
|---|---|
| setor | Escritório da SEC responsável pela empresa (`ownerOrg`), traduzido |
| subsetor | Grupo SIC de 2 dígitos |
| segmento | Descrição SIC de 4 dígitos |

`tipo_especial` marca `financeira` (SIC 60–64), `REIT` (6798) e `SPAC` (6770). SPACs são excluídas.

## Metodologia — diferenças em relação à base BR

1. **Escolha de tags.** Cada conceito tem tags alternativas em ordem de prioridade (`conceitos_eua.py`). Dentro de cada exercício usa-se uma única alternativa, para que trimestres derivados não misturem tags. Se for preciso completar lacunas com outra tag, a auditoria registra "(misto)".
2. **Trimestres.** Usa-se o 3M reportado. Na falta dele, YTD diferenciado (6M−3M, 9M−6M), e o 4T vem de anual − 9M.
3. **LTM.** É a soma de 4 trimestres contíguos. No 4T, recorre ao anual se faltar algum trimestre.
4. **Exercício.** O rótulo é o ano civil do encerramento. Anos de 52/53 semanas que fecham na 1ª semana de janeiro contam no ano anterior. Exemplo: Walmart, ano fiscal encerrado em jan/2026 = 2026.
5. **Reapresentações e point-in-time.** O valor é o do arquivamento mais recente (10-K/A ou comparativo posterior). A coluna `dt_arquivamento` guarda a data da **primeira** divulgação do período; comparativos e 10-K/A posteriores não contam. É ela que define o que estava público em cada data para os múltiplos.
6. **Arrendamentos (ASC 842 ≠ IFRS 16).**
   - O aluguel operacional continua no opex.
   - `ebitda` é o US GAAP reportado (EBIT + D&A + impairment).
   - `ebitda_pre_ifrs16` = EBITDA − pagamentos de arrendamento **financeiro** (principal + juros), ou seja, o EBITDA após todos os arrendamentos.
   - `ebitda_ifrs16_equiv` = EBITDA + custo de arrendamento operacional (quando divulgado). Equivale ao conceito IFRS 16.
   - `div_liq_ebitda_ifrs16` usa a dívida com todos os arrendamentos sobre `ebitda_ifrs16_equiv`. `div_liq_ebitda_pre_ifrs16` usa a dívida sem arrendamentos sobre `ebitda_pre_ifrs16`.
7. **Dívida.** Usa-se LongTermDebt (inclui parcela corrente) + ShortTermBorrowings + CommercialPaper, com alternativas em `conceitos_eua.py`. Arrendamentos ficam em `passivo_arrendamento`.
8. **Caixa.** Caixa e equivalentes + aplicações de curto prazo.
9. **Ações (regra de market cap com todas as classes).** Em empresas multiclasse, o dado de capa (dei) em geral não vem no companyfacts. Por isso a ordem é capa > `CommonStockSharesOutstanding` (soma das classes) > média básica 3M. Se a capa for menor que 70% do balanço, ela é tratada como parcial. Saltos acima de 5x são marcados em `alerta_salto`.
10. **Colunas novas (EUA).** `recompra_acoes`, `emissao_acoes` e `retorno_acionista_caixa` (dividendos + recompras − emissões).

## Limitações conhecidas

- **Bancos e seguradoras.** Receita/EBITDA não se aplicam; ficam marcados como `financeira` e precisam de módulo próprio.
- **Juros pagos e IR pagos.** Muitas empresas só divulgam no 10-K, então os índices de cobertura trimestrais/LTM ficam vazios nesses casos.
- **Emissores estrangeiros.** Quem arquiva 20-F/40-F (IFRS) está fora.
- **Preços.** A SEC não fornece preços. `sonda_precos.py` testa fontes gratuitas a partir do Actions antes de construirmos os múltiplos.

## Testes

```
python testes/teste_coletor.py
```

São casos sintéticos: troca de tag, 10-K/A, YTD, 10-Q faltante, exercício em andamento e multiclasse. O workflow aborta se algum falhar.
