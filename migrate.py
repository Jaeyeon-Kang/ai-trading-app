#!/usr/bin/env python3
"""
PostgreSQL 데이터베이스 마이그레이션 스크립트
Week 1 라이트봇 A단계 - DB 스키마 적용
"""

import os
import sys
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import logging

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_db_config():
    """환경변수에서 DB 설정 가져오기"""
    return {
        'host': os.getenv('POSTGRES_HOST', 'localhost'),
        'port': os.getenv('POSTGRES_PORT', '5432'),
        'database': os.getenv('POSTGRES_DB', 'trading_bot'),
        'user': os.getenv('POSTGRES_USER', 'trading_bot_user'),
        'password': os.getenv('POSTGRES_PASSWORD', 'trading_bot_password')
    }

def create_database_if_not_exists():
    """데이터베이스가 없으면 생성"""
    config = get_db_config()
    
    # postgres 데이터베이스에 연결 (기본 DB)
    conn = psycopg2.connect(
        host=config['host'],
        port=config['port'],
        database='postgres',
        user=config['user'],
        password=config['password']
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    
    cursor = conn.cursor()
    
    try:
        # 데이터베이스 존재 여부 확인
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (config['database'],))
        exists = cursor.fetchone()
        
        if not exists:
            logger.info(f"데이터베이스 {config['database']} 생성 중...")
            cursor.execute(f"CREATE DATABASE {config['database']}")
            logger.info(f"데이터베이스 {config['database']} 생성 완료")
        else:
            logger.info(f"데이터베이스 {config['database']} 이미 존재")
            
    except Exception as e:
        logger.error(f"데이터베이스 생성 중 오류: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

def run_migration():
    """스키마 마이그레이션 실행"""
    config = get_db_config()
    
    try:
        # 대상 데이터베이스에 연결
        conn = psycopg2.connect(**config)
        cursor = conn.cursor()
        
        logger.info("스키마 파일 읽는 중...")
        
        # schema.sql 파일 읽기
        schema_path = os.path.join('app', 'db', 'schema.sql')
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_sql = f.read()
        
        logger.info("스키마 적용 중...")
        
        # SQL 실행
        cursor.execute(schema_sql)
        conn.commit()
        
        logger.info("스키마 적용 완료")
        
        # 테이블 생성 확인
        verify_tables(cursor)
        
    except Exception as e:
        logger.error(f"마이그레이션 중 오류: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def verify_tables(cursor):
    """필수 테이블들이 제대로 생성되었는지 확인"""
    logger.info("테이블 생성 확인 중...")
    
    required_tables = [
        'edgar_events',
        'bars_30s', 
        'signals',
        'orders_paper',
        'fills_paper',
        'metrics_daily'
    ]
    
    for table in required_tables:
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = %s
            )
        """, (table,))
        
        exists = cursor.fetchone()[0]
        if exists:
            logger.info(f"✓ {table} 테이블 존재")
        else:
            logger.error(f"✗ {table} 테이블 없음")
            raise Exception(f"필수 테이블 {table}이 생성되지 않았습니다")
    
    # 인덱스 확인
    logger.info("인덱스 확인 중...")
    cursor.execute("""
        SELECT indexname FROM pg_indexes 
        WHERE tablename IN ('edgar_events', 'bars_30s', 'signals', 'orders_paper', 'fills_paper', 'metrics_daily')
        AND schemaname = 'public'
    """)
    
    indexes = [row[0] for row in cursor.fetchall()]
    logger.info(f"생성된 인덱스: {len(indexes)}개")
    
    # 뷰 확인
    logger.info("뷰 확인 중...")
    cursor.execute("""
        SELECT viewname FROM pg_views 
        WHERE schemaname = 'public' 
        AND viewname LIKE '%summary%'
    """)
    
    views = [row[0] for row in cursor.fetchall()]
    logger.info(f"생성된 뷰: {views}")
    
    logger.info("모든 테이블과 인덱스가 정상적으로 생성되었습니다")

def test_connection():
    """데이터베이스 연결 테스트"""
    config = get_db_config()
    
    try:
        conn = psycopg2.connect(**config)
        cursor = conn.cursor()
        
        cursor.execute("SELECT version()")
        version = cursor.fetchone()[0]
        logger.info(f"PostgreSQL 연결 성공: {version}")
        
        cursor.close()
        conn.close()
        return True
        
    except Exception as e:
        logger.error(f"데이터베이스 연결 실패: {e}")
        return False

def main():
    """메인 함수"""
    logger.info("=== Week 1 라이트봇 A단계 - DB 마이그레이션 시작 ===")
    
    try:
        # 1. 연결 테스트
        if not test_connection():
            logger.error("데이터베이스 연결에 실패했습니다. docker-compose가 실행 중인지 확인하세요.")
            sys.exit(1)
        
        # 2. 데이터베이스 생성 (필요시)
        create_database_if_not_exists()
        
        # 3. 마이그레이션 실행
        run_migration()
        
        logger.info("=== 마이그레이션 완료 ===")
        logger.info("이제 라이트봇 A단계 개발을 시작할 수 있습니다.")
        
    except Exception as e:
        logger.error(f"마이그레이션 실패: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
