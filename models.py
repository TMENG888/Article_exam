# models.py
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Optional


class QuestionSchema(BaseModel):
    id: int
    # 使用 pattern 替代 regex
    type: str = Field(..., pattern=r'^(choice|fill_blank|short_answer|open_ended)$')
    level: str = Field(..., pattern=r'^(basic|apply|advanced)$')
    question: str
    options: Optional[Dict[str, str]] = None
    answer: str
    explanation: str
    evidence_source: Optional[str] = ""

    @field_validator('options') # 使用 field_validator
    def validate_options(cls, v, values):
        if values.data.get('type') == 'choice': # Pydantic V2 中访问方式略有不同
            if not v or len(v) < 2:
                raise ValueError('Choice questions must have at least 2 options.')
        return v


class ExamPaperSchema(BaseModel):
    """用于 LLM 任务 A 的结构化输出校验"""
    title: str = Field(..., max_length=20)
    summary: str = Field(..., min_length=50, max_length=500)
    # 使用 min_items 替代 min_items
    questions: List[QuestionSchema] = Field(..., min_length=1)

    @field_validator('questions')
    def validate_questions(cls, v):
        if len(v) == 0:
            raise ValueError('Exam paper must contain at least one question.')
        # 可以添加更多逻辑，比如检查是否有重复ID等
        ids = [q.id for q in v]
        if len(ids) != len(set(ids)):
             raise ValueError('Question IDs must be unique.')
        return v


class GenerateRequest(BaseModel):
    article_text: str = Field(..., min_length=100)
    pass_score: int = Field(default=60, ge=0, le=100)
    api_config: Optional[dict] = None
    extract_model_index: Optional[int] = None  # 🔑 知识提取阶段模型索引
    generate_model_index: Optional[int] = None # 🔑 正式出题阶段模型索引


class GradeRequest(BaseModel):
    session_id: str
    questions: list
    answers: dict
    pass_score: int = Field(default=60, ge=0, le=100)
    api_config: Optional[dict] = None


class SaveProgressRequest(BaseModel):
    session_id: str
    answers: dict


class ConfigRequest(BaseModel):
    api_base_url: str = ""
    api_key: str = ""
    model: str = ""
    model_index: Optional[int] = None