-- ══════════════════════════════════════════════════════════════
-- ConversaScore - Schema para PostgreSQL (n8n_queue database)
-- Rodar no servidor: psql -U postgres -d n8n_queue -f schema.sql
-- ══════════════════════════════════════════════════════════════

-- Schema separado para não misturar com tabelas do n8n
CREATE SCHEMA IF NOT EXISTS conversascore;

-- ─── Tabela de conversas ──────────────────────────────────────
-- Cada conversa do Chatwoot que passou por handoff para vendedor
CREATE TABLE IF NOT EXISTS conversascore.conversas (
    id SERIAL PRIMARY KEY,
    chatwoot_conversation_id INTEGER NOT NULL UNIQUE,
    chatwoot_contact_id INTEGER,
    cliente_nome VARCHAR(255),
    cliente_telefone VARCHAR(50),
    vendedor_agent_id INTEGER NOT NULL,
    vendedor_nome VARCHAR(255),
    status VARCHAR(50) DEFAULT 'aberta',  -- aberta, finalizada, scored
    handoff_at TIMESTAMP NOT NULL,         -- quando o bot transferiu pro vendedor
    primeira_resposta_at TIMESTAMP,        -- quando o vendedor respondeu pela 1ª vez
    ultima_mensagem_at TIMESTAMP,
    finalizada_at TIMESTAMP,
    total_mensagens_cliente INTEGER DEFAULT 0,
    total_mensagens_vendedor INTEGER DEFAULT 0,
    total_audios_transcritos INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ─── Tabela de mensagens ──────────────────────────────────────
-- Armazena apenas mensagens pós-handoff (ignora chatbot)
CREATE TABLE IF NOT EXISTS conversascore.mensagens (
    id SERIAL PRIMARY KEY,
    conversa_id INTEGER NOT NULL REFERENCES conversascore.conversas(id) ON DELETE CASCADE,
    chatwoot_message_id INTEGER,
    remetente VARCHAR(20) NOT NULL,        -- 'cliente' ou 'vendedor'
    tipo VARCHAR(20) DEFAULT 'texto',      -- texto, audio, imagem, arquivo
    conteudo TEXT,                          -- texto da mensagem ou transcrição do áudio
    audio_url TEXT,                         -- URL do áudio original (se aplicável)
    audio_duracao_seg INTEGER,             -- duração do áudio em segundos
    is_mensagem_bot BOOLEAN DEFAULT FALSE, -- flag pra mensagens de recuperação do bot
    enviada_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ─── Tabela de scores ─────────────────────────────────────────
-- Score calculado por conversa
CREATE TABLE IF NOT EXISTS conversascore.scores (
    id SERIAL PRIMARY KEY,
    conversa_id INTEGER NOT NULL UNIQUE REFERENCES conversascore.conversas(id) ON DELETE CASCADE,
    
    -- Critério 1: Velocidade de Resposta (peso 25%)
    velocidade_nota DECIMAL(4,2),          -- 0 a 10
    velocidade_tempo_min DECIMAL(10,2),    -- tempo real em minutos
    
    -- Critério 2: Continuidade / Follow-up (peso 20%)
    followup_nota DECIMAL(4,2),            -- 0 a 10
    followup_retomou BOOLEAN,
    followup_tempo_horas DECIMAL(10,2),    -- tempo até retomar
    
    -- Critério 3: Qualidade Comercial (peso 25%) - IA
    qualidade_nota DECIMAL(4,2),           -- 0 a 10
    qualidade_analise TEXT,                -- justificativa da IA
    
    -- Critério 4: Tom e Personalização (peso 15%) - IA
    tom_nota DECIMAL(4,2),                 -- 0 a 10
    tom_analise TEXT,                      -- justificativa da IA
    
    -- Critério 5: Resolução / Resultado (peso 15%)
    resultado_nota DECIMAL(4,2),           -- 0 a 10
    resultado_status VARCHAR(50),          -- convertido, perdido, em_andamento
    
    -- Score total ponderado
    score_total DECIMAL(5,2),              -- 0 a 100
    
    -- Metadata
    analisado_por VARCHAR(50) DEFAULT 'claude-api',
    tokens_consumidos INTEGER,
    scored_at TIMESTAMP DEFAULT NOW()
);

-- ─── Tabela de insights mensais ───────────────────────────────
CREATE TABLE IF NOT EXISTS conversascore.insights (
    id SERIAL PRIMARY KEY,
    mes INTEGER NOT NULL,
    ano INTEGER NOT NULL,
    vendedor_nome VARCHAR(255),            -- NULL = insight do time
    tipo VARCHAR(20) NOT NULL,             -- positivo, atencao, sugestao
    texto TEXT NOT NULL,
    gerado_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(mes, ano, vendedor_nome, texto)
);

-- ─── Índices para performance ─────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_conversas_vendedor ON conversascore.conversas(vendedor_agent_id);
CREATE INDEX IF NOT EXISTS idx_conversas_status ON conversascore.conversas(status);
CREATE INDEX IF NOT EXISTS idx_conversas_handoff ON conversascore.conversas(handoff_at);
CREATE INDEX IF NOT EXISTS idx_mensagens_conversa ON conversascore.mensagens(conversa_id);
CREATE INDEX IF NOT EXISTS idx_mensagens_enviada ON conversascore.mensagens(enviada_at);
CREATE INDEX IF NOT EXISTS idx_scores_conversa ON conversascore.scores(conversa_id);
CREATE INDEX IF NOT EXISTS idx_scores_scored ON conversascore.scores(scored_at);
CREATE INDEX IF NOT EXISTS idx_insights_periodo ON conversascore.insights(ano, mes);

-- ─── Trigger para atualizar updated_at ────────────────────────
CREATE OR REPLACE FUNCTION conversascore.update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_conversas_updated ON conversascore.conversas;
CREATE TRIGGER trg_conversas_updated
    BEFORE UPDATE ON conversascore.conversas
    FOR EACH ROW EXECUTE FUNCTION conversascore.update_timestamp();
