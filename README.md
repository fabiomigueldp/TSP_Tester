# TSP Tester - Trading Signal Processor Tester

**Versão 2.2** - Ferramenta Profissional de Teste para o Trading-Signal-Processor

## Descrição

Esta aplicação é um utilitário completo de teste de carga e funcionalidade, desenhado especificamente para o microserviço Trading-Signal-Processor.

## Funcionalidades Principais

- ✅ **Envio de sinais** com tickers pré-definidos para teste de aprovação e rejeição
- ✅ **Padrão de envio suave** - requisições distribuídas ao longo do tempo
- ✅ **Controle de proporção** entre sinais aprovados vs. rejeitados
- ✅ **Templating dinâmico** de payload com Faker para dados realistas
- ✅ **Controles avançados** de RPS, período de ramp-up e duração do teste
- ✅ **Monitoramento em tempo real** do estado interno da aplicação via API polling
- ✅ **Verificação de integridade** comparando dados enviados vs. processados
- ✅ **Gráficos em tempo real** de RPS, latência, tamanho da fila e contagens de sinais
- ✅ **Painel de controle** para interagir com endpoints de admin em tempo real
- ✅ **Gerenciamento de perfis** de teste (salvar/carregar)
- ✅ **Exportação de resultados** detalhados em CSV
- ✅ **Modo Headless/CLI** para testes automatizados

## Tecnologias Utilizadas

- **Python 3.7+**
- **Tkinter** para interface gráfica
- **Matplotlib** para gráficos em tempo real
- **httpx** para requisições HTTP assíncronas
- **asyncio** para programação assíncrona
- **Faker** para geração de dados de teste

## Pré-requisitos

```bash
pip install httpx faker matplotlib
```

## Como Usar

### Interface Gráfica
```bash
python tsp_tester.py
```

### Modo CLI/Headless
```bash
python tsp_tester.py --headless --config config.json
```

## Configuração

O TSP Tester permite configurar diversos parâmetros:

- **URL do serviço**: Endpoint do Trading-Signal-Processor
- **RPS (Requests Per Second)**: Taxa de requisições por segundo
- **Duração do teste**: Tempo total do teste em segundos
- **Ramp-up**: Período de aceleração gradual das requisições
- **Proporção de aprovação**: Percentual de sinais que devem ser aprovados
- **Tickers aprovados/rejeitados**: Lista de símbolos para teste
- **Template de payload**: Formato JSON personalizado para os sinais

## Recursos Avançados

### Gráficos em Tempo Real
- RPS (Requisições por segundo)
- Latência média
- Tamanho da fila do processador
- Contadores de sinais aprovados/rejeitados

### Painel Administrativo
Controle direto dos endpoints administrativos do Trading-Signal-Processor:
- Reset de estatísticas
- Configuração de parâmetros
- Monitoramento de estado

### Perfis de Teste
- Salvar configurações em arquivos JSON
- Carregar perfis pré-configurados
- Compartilhar configurações entre equipes

## Estrutura do Projeto

```
TSP_Tester/
├── tsp_tester.py          # Aplicação principal
├── README.md              # Documentação
└── profiles/              # Perfis de teste salvos (criado automaticamente)
```

## Contribuição

1. Fork o projeto
2. Crie uma branch para sua feature (`git checkout -b feature/AmazingFeature`)
3. Commit suas mudanças (`git commit -m 'Add some AmazingFeature'`)
4. Push para a branch (`git push origin feature/AmazingFeature`)
5. Abra um Pull Request

## Licença

Este projeto está sob a licença MIT. Veja o arquivo `LICENSE` para mais detalhes.

## Autor

**Fabio Miguel** - [GitHub](https://github.com/fabiomigueldp)

---

**TSP Tester v2.2** - Teste profissional para Trading Signal Processor
