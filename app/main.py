"""Employee Attendance & Analytics API (HROne engineering assignment)."""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId

load_dotenv(override=False)
client = MongoClient(os.getenv('MONGO_URI', 'mongodb://localhost:27017'), serverSelectionTimeoutMS=5000)
db = client[os.getenv('MONGO_DB', 'attendance_db')]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ('PRESENT', 'WFH', 'ON_DUTY')
Status = Literal['PRESENT', 'ABSENT', 'LEAVE', 'WFH', 'ON_DUTY']
PresenceStatus = Literal['PRESENT', 'WFH', 'ON_DUTY']

@asynccontextmanager
async def lifespan(app):
    db.employees.create_index([('emp_code', ASCENDING)], unique=True)
    db.employees.create_index([('department', ASCENDING), ('joined_on', ASCENDING)])
    db.attendance_logs.create_index([('emp_code', ASCENDING), ('date', ASCENDING)], unique=True)
    db.attendance_logs.create_index([('date', DESCENDING), ('emp_code', ASCENDING)])
    db.attendance_logs.create_index([('date', ASCENDING), ('late_minutes', DESCENDING)])
    db.attendance_logs.create_index([('status', ASCENDING), ('date', DESCENDING), ('emp_code', ASCENDING)])
    yield

app = FastAPI(title='Employee Attendance & Analytics API', version='2.0.0', lifespan=lifespan)

def fail(code, message):
    raise HTTPException(status_code=code, detail=message)

def ms(dt):
    if dt is None: return None
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)

def instant(value=None):
    if value is None: return datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0, tzinfo=None)

def day_of(dt):
    return dt.replace(tzinfo=UTC).astimezone(IST).date()

def local_shift(d, hhmm):
    h, m = map(int, hhmm.split(':'))
    return datetime.combine(d, time(h, m), IST).astimezone(UTC).replace(tzinfo=None)

def attendance_date(dt, emp):
    d = day_of(dt)
    if emp['shift_end'] <= emp['shift_start'] and dt < local_shift(d, emp['shift_end']):
        return d - timedelta(days=1)
    return d

def rounded(x, places=2):
    return float(Decimal(str(x)).quantize(Decimal('1').scaleb(-places), rounding=ROUND_HALF_UP))

def derived(emp, d, status, pin, pout):
    if status not in PRESENCE or pin is None:
        return dict(late_minutes=0, overtime_minutes=0, work_hours=None, half_day=False)
    start = local_shift(d, emp['shift_start'])
    late_seconds = (pin - start).total_seconds()
    late = int(late_seconds // 60) if late_seconds > 600 else 0
    if pout is None:
        return dict(late_minutes=late, overtime_minutes=0, work_hours=None, half_day=False)
    if pout <= pin or pout - pin > timedelta(hours=24): fail(422, 'punch_out must be after punch_in and within 24 hours')
    end_date = d + timedelta(days=1) if emp['shift_end'] <= emp['shift_start'] else d
    end = local_shift(end_date, emp['shift_end'])
    overtime = max(0, int((pout-end).total_seconds() // 60))
    hours = rounded(Decimal(str((pout-pin).total_seconds())) / Decimal(3600))
    return dict(late_minutes=late, overtime_minutes=overtime if overtime >= 30 else 0,
                work_hours=hours, half_day=hours < 4.50)

def serialize(doc):
    d = dict(doc)
    d.pop('_id', None)
    for field in ('punch_in', 'punch_out', 'created_at'):
        if isinstance(d.get(field), datetime): d[field] = ms(d[field])
    if 'history' in d:
        history=[]
        for entry in d.get('history') or []:
            e=dict(entry)
            if isinstance(e.get('at'), datetime): e['at']=ms(e['at'])
            for name in ('punch_in','punch_out'):
                if name in e.get('changes', {}):
                    for key in ('from','to'):
                        val=e['changes'][name].get(key)
                        if isinstance(val, datetime): e['changes'][name][key]=ms(val)
            history.append(e)
        d['history']=history
    if 'date' in d:
        d.setdefault('history', [])
        d.setdefault('half_day', False)
    return d

def employee(code):
    e=db.employees.find_one({'emp_code':code})
    if e is None: fail(404, 'Employee not found')
    return e

def month_bounds(month):
    y,m=map(int,month.split('-'))
    start=date(y,m,1)
    end=date(y,m,calendar.monthrange(y,m)[1])
    return start,end

def working_days(start,end):
    if end<start:return 0
    days=(end-start).days+1
    full, rem=divmod(days,7)
    return full*5+sum((start.weekday()+i)%7<5 for i in range(rem))

def match_month(month):
    return {'date':{'$gte':month+'-01','$lte':month+'-31'}}

def present_expr():
    return {'$cond':[{'$and':[{'$in':['$status',list(PRESENCE)]}, {'$in':[{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},[2,3,4,5,6]]}]}, {'$cond':[{'$ifNull':['$half_day',False]},0.5,1]},0]}

class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    emp_code: str=Field(pattern=r'^EMP\d{4,6}$')
    name: str=Field(min_length=1,max_length=100)
    email: str=Field(max_length=120,pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str=Field(min_length=1,max_length=50)
    shift_start: str=Field(default='09:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str=Field(default='18:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: date
    @model_validator(mode='after')
    def shifts(self):
        if self.shift_start==self.shift_end: raise ValueError('shift_start and shift_end must differ')
        return self

class PunchInIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    emp_code: str
    punched_at: Optional[int]=Field(default=None,ge=100000000000,le=4102444800000)
    status: PresenceStatus='PRESENT'

class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    emp_code: str
    punched_at: Optional[int]=Field(default=None,ge=100000000000,le=4102444800000)

class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reason: str=Field(min_length=5,max_length=200)
    regularized_by: str=Field(min_length=1,max_length=50)
    status: Optional[Status]=None
    punch_in: Optional[int]=Field(default=None,ge=100000000000,le=4102444800000)
    punch_out: Optional[int]=Field(default=None,ge=100000000000,le=4102444800000)

@app.get('/health')
def health():
    try: db.command('ping')
    except Exception: fail(503, 'MongoDB unavailable')
    return {'status':'ok'}

@app.post('/employees',status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(mode='json')
    doc['created_at']=instant()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: fail(409,'emp_code already exists')
    return serialize(doc)

@app.get('/employees')
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={'department':department} if department else {}
    docs=db.employees.find(q,{'_id':0}).sort('emp_code',1).skip((page-1)*page_size).limit(page_size)
    return {'items':[serialize(x) for x in docs], 'total':db.employees.count_documents(q),'page':page,'page_size':page_size}

@app.post('/attendance/punch-in',status_code=201)
def punch_in(body:PunchInIn):
    emp=employee(body.emp_code)
    ts=instant(body.punched_at)
    d=attendance_date(ts,emp)
    doc={'emp_code':body.emp_code,'date':d.isoformat(),'status':body.status,
         'punch_in':ts,'punch_out':None,'history':[],**derived(emp,d,body.status,ts,None)}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: fail(409,'already punched in for this date')
    return serialize(doc)

@app.post('/attendance/punch-out')
def punch_out(body:PunchOutIn):
    emp=employee(body.emp_code)
    ts=instant(body.punched_at)
    record=db.attendance_logs.find_one({'emp_code':body.emp_code,'punch_in':{'$lte':ts}},sort=[('punch_in',-1)])
    if record is None: fail(404,'No punch-in found')
    if record.get('punch_out') is not None: fail(409,'Already punched out')
    if ts<=record['punch_in'] or ts-record['punch_in']>timedelta(hours=24):fail(422,'Invalid punch-out time')
    changes=derived(emp,date.fromisoformat(record['date']),record['status'],record['punch_in'],ts)
    updated=db.attendance_logs.find_one_and_update({'_id':record['_id'],'punch_out':None},
        {'$set':{'punch_out':ts,**changes}},return_document=ReturnDocument.AFTER)
    if updated is None: fail(409,'Already punched out')
    return serialize(updated)

@app.get('/attendance')
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[date]=None,date_to:Optional[date]=None,
                    status:Optional[Status]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from and date_to and date_from>date_to:fail(422,'date_from must be <= date_to')
    q={}
    if emp_code:q['emp_code']=emp_code
    if status:q['status']=status
    if date_from or date_to:
        q['date']={}
        if date_from:q['date']['$gte']=date_from.isoformat()
        if date_to:q['date']['$lte']=date_to.isoformat()
    docs=db.attendance_logs.find(q).sort([('date',-1),('emp_code',1)]).skip((page-1)*page_size).limit(page_size)
    return {'items':[serialize(x) for x in docs],'total':db.attendance_logs.count_documents(q),'page':page,'page_size':page_size}

@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code:str,date:date,body:RegularizeIn):
    emp=employee(emp_code)
    record=db.attendance_logs.find_one({'emp_code':emp_code,'date':date.isoformat()})
    if record is None:fail(404,'Attendance record not found')
    supplied=body.model_fields_set
    status=body.status if 'status' in supplied else record['status']
    if status is None:fail(422,'Invalid status')
    if status in ('ABSENT','LEAVE'):
        if 'punch_in' in supplied or 'punch_out' in supplied:fail(422,'Absence/leave cannot include punch times')
        pin=pout=None
    else:
        pin=instant(body.punch_in) if body.punch_in is not None else (None if 'punch_in' in supplied else record.get('punch_in'))
        pout=instant(body.punch_out) if body.punch_out is not None else (None if 'punch_out' in supplied else record.get('punch_out'))
        if pin is None:fail(422,'Presence requires punch_in')
        if attendance_date(pin,emp)!=date:fail(422,'punch_in must belong to record date')
    updated={'status':status,'punch_in':pin,'punch_out':pout,**derived(emp,date,status,pin,pout)}
    changes={}
    for key,new in updated.items():
        old=record.get(key,False if key=='half_day' else None)
        if old!=new:changes[key]={'from':ms(old) if key in ('punch_in','punch_out') else old,
                                  'to':ms(new) if key in ('punch_in','punch_out') else new}
    if not changes:fail(422,'No changes supplied')
    entry={'at':instant(),'by':body.regularized_by,'reason':body.reason,'changes':changes}
    # Compare the original state so competing edits cannot overwrite each other.
    filter_q={'_id':record['_id']}
    for key in updated:filter_q[key]=record[key] if key in record else {'$exists':False}
    result=db.attendance_logs.find_one_and_update(filter_q,{'$set':updated,'$push':{'history':entry}},return_document=ReturnDocument.AFTER)
    if result is None:fail(409,'Record changed concurrently; retry')
    return serialize(result)

@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code:str,month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp=employee(emp_code)
    start,end=month_bounds(month)
    joined=date.fromisoformat(emp['joined_on'])
    days=working_days(max(start,joined),end)
    pipeline=[{'$match':{'emp_code':emp_code,**match_month(month)}},
      {'$group':{'_id':None,'present_days':{'$sum':present_expr()},
        'leave_days':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},
        'late_count':{'$sum':{'$cond':[{'$gt':['$late_minutes',0]},1,0]}},
        'total_late_minutes':{'$sum':'$late_minutes'},'total_overtime_minutes':{'$sum':'$overtime_minutes'}}}]
    result=list(db.attendance_logs.aggregate(pipeline))
    x=result[0] if result else {}
    present=rounded(x.get('present_days',0))
    return {'emp_code':emp_code,'month':month,'working_days':days,'present_days':present,
      'leave_days':x.get('leave_days',0),'late_count':x.get('late_count',0),
      'total_late_minutes':x.get('total_late_minutes',0),'total_overtime_minutes':x.get('total_overtime_minutes',0),
      'attendance_pct':rounded(Decimal(str(present))/Decimal(days)*100) if days else None}

@app.get('/analytics/departments/summary')
def department_summary(month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),department:Optional[str]=None):
    start,end=month_bounds(month)
    q={'joined_on':{'$lte':end.isoformat()}}
    if department:q['department']=department
    pipeline=[{'$match':q},{'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code'},'pipeline':[
        {'$match':{'$expr':{'$eq':['$emp_code','$$code']},**match_month(month)}},
        {'$project':{'status':1,'late_minutes':1,'work_hours':1,'present':present_expr()}}], 'as':'logs'}},
      {'$unwind':{'path':'$logs','preserveNullAndEmptyArrays':True}},
      {'$group':{'_id':'$department','headcount_codes':{'$addToSet':'$emp_code'},
        'present_days':{'$sum':{'$ifNull':['$logs.present',0]}},
        'work_sum':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',list(PRESENCE)]},{'$ne':['$logs.work_hours',None]}]},'$logs.work_hours',0]}},
        'work_count':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',list(PRESENCE)]},{'$ne':['$logs.work_hours',None]}]},1,0]}},
        'late_count':{'$sum':{'$cond':[{'$gt':['$logs.late_minutes',0]},1,0]}},
        'total_late_minutes':{'$sum':{'$ifNull':['$logs.late_minutes',0]}},
        'leave_count':{'$sum':{'$cond':[{'$eq':['$logs.status','LEAVE']},1,0]}},
        'on_duty_count':{'$sum':{'$cond':[{'$eq':['$logs.status','ON_DUTY']},1,0]}}}},
      {'$sort':{'_id':1}}]
    items=[]
    for x in db.employees.aggregate(pipeline):
        items.append({'department':x['_id'],'headcount':len(x['headcount_codes']),
          'present_days':rounded(x['present_days']),
          'avg_work_hours':rounded(Decimal(str(x['work_sum']))/Decimal(x['work_count'])) if x['work_count'] else None,
          'late_count':x['late_count'],'total_late_minutes':x['total_late_minutes'],
          'leave_count':x['leave_count'],'on_duty_count':x['on_duty_count']})
    return {'month':month,'items':items}

@app.get('/analytics/leaderboard/late')
def late_leaderboard(month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    pipeline=[{'$match':match_month(month)},
      {'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':'$late_minutes'},
        'late_count':{'$sum':{'$cond':[{'$gt':['$late_minutes',0]},1,0]}}}},
      {'$match':{'total_late_minutes':{'$gt':0}}},
      {'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'emp'}},
      {'$unwind':'$emp'}]
    if department:pipeline.append({'$match':{'emp.department':department}})
    pipeline += [{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},
      {'$match':{'rank':{'$lte':limit}}},
      {'$sort':{'total_late_minutes':-1,'_id':1}},
      {'$project':{'_id':0,'rank':1,'emp_code':'$_id','name':'$emp.name','department':'$emp.department',
         'total_late_minutes':1,'late_count':1}}]
    return {'month':month,'items':list(db.attendance_logs.aggregate(pipeline))}

@app.get('/analytics/departments/{department}/trend')
def department_trend(department:str,from_:date=Query(...,alias='from'),to:date=Query(...)):
    if to<from_ or (to-from_).days>=92:fail(422,'Date range must be 1-92 days')
    if not db.employees.find_one({'department':department}):fail(404,'Department not found')
    # Seed the range with a synthetic day and fill the remaining dates inside MongoDB.
    start=datetime.combine(from_,time.min)
    end=datetime.combine(to,time.min)
    pipeline=[{'$documents':[{'day':start}]},
      {'$densify':{'field':'day','range':{'step':1,'unit':'day','bounds':[start,end+timedelta(days=1)]}}},
      {'$match':{'day':{'$lte':end}}},
      {'$set':{'date':{'$dateToString':{'format':'%Y-%m-%d','date':'$day'}},
        'is_working_day':{'$in':[{'$dayOfWeek':'$day'},[2,3,4,5,6]]}}},
      {'$lookup':{'from':'employees','let':{'d':'$date'},'pipeline':[
        {'$match':{'department':department,'$expr':{'$lte':['$joined_on','$$d']}}},
        {'$count':'n'}], 'as':'hc'}},
      {'$lookup':{'from':'attendance_logs','let':{'d':'$date'},'pipeline':[
        {'$match':{'$expr':{'$eq':['$date','$$d']}}},
        {'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'emp'}},
        {'$unwind':'$emp'}, {'$match':{'emp.department':department}},
        {'$group':{'_id':None,'present_count':{'$sum':{'$cond':[{'$in':['$status',list(PRESENCE)]},
            {'$cond':[{'$ifNull':['$half_day',False]},0.5,1]},0]}},
            'late_count':{'$sum':{'$cond':[{'$gt':['$late_minutes',0]},1,0]}}}}], 'as':'logs'}},
      {'$set':{'headcount':{'$ifNull':[{'$arrayElemAt':['$hc.n',0]},0]},
        'present_count':{'$ifNull':[{'$arrayElemAt':['$logs.present_count',0]},0]},
        'late_count':{'$ifNull':[{'$arrayElemAt':['$logs.late_count',0]},0]}}},
      {'$set':{'attendance_rate':{'$cond':[{'$and':['$is_working_day',{'$gt':['$headcount',0]}]},
        {'$round':[{'$divide':['$present_count','$headcount']},4]},None]}}},
      {'$setWindowFields':{'sortBy':{'day':1},'output':{'moving_avg_7d':{
        '$avg':'$attendance_rate','window':{'documents':[-6,0]}}}}},
      {'$project':{'_id':0,'date':1,'is_working_day':1,'headcount':1,'present_count':1,
        'late_count':1,'attendance_rate':1,'moving_avg_7d':1}}]
    items=list(db.employees.aggregate(pipeline))
    for item in items:
        if item['attendance_rate'] is not None:item['attendance_rate']=rounded(item['attendance_rate'],4)
        if item.get('moving_avg_7d') is not None:item['moving_avg_7d']=rounded(item['moving_avg_7d'],4)
    return {'department':department,'items':items}

@app.get('/admin/explain/{endpoint}')
def explain_endpoint(endpoint:Literal['attendance_list','employee_monthly','department_summary','late_leaderboard','department_trend'],
    emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,
    date_from:Optional[date]=None,date_to:Optional[date]=None,status:Optional[Status]=None,
    page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),limit:int=Query(10,ge=1,le=50),
    from_:Optional[date]=Query(None,alias='from'),to:Optional[date]=None):
    if endpoint=='attendance_list':
        q={}
        if emp_code:q['emp_code']=emp_code
        if status:q['status']=status
        if date_from or date_to:
            q['date']={}
            if date_from:q['date']['$gte']=date_from.isoformat()
            if date_to:q['date']['$lte']=date_to.isoformat()
        collection='attendance_logs'
        command={'find':collection,'filter':q,'sort':{'date':-1,'emp_code':1},'skip':(page-1)*page_size,'limit':page_size}
    elif endpoint=='employee_monthly':
        if not emp_code or not month:fail(422,'emp_code and month required')
        collection='attendance_logs';command={'aggregate':collection,'pipeline':[{'$match':{'emp_code':emp_code,**match_month(month)}},{'$group':{'_id':None,'count':{'$sum':1}}}],'cursor':{}}
    elif endpoint=='department_summary':
        if not month:fail(422,'month required')
        collection='employees';q={'joined_on':{'$lte':month+'-31'}}
        if department:q['department']=department
        command={'aggregate':collection,'pipeline':[{'$match':q},{'$group':{'_id':'$department','headcount':{'$sum':1}}}],'cursor':{}}
    elif endpoint=='late_leaderboard':
        if not month:fail(422,'month required')
        collection='attendance_logs';command={'aggregate':collection,'pipeline':[{'$match':match_month(month)},{'$group':{'_id':'$emp_code','late':{'$sum':'$late_minutes'}}}],'cursor':{}}
    else:
        if not department or not from_ or not to:fail(422,'department, from, to required')
        collection='employees';command={'aggregate':collection,'pipeline':[{'$match':{'department':department,'joined_on':{'$lte':to.isoformat()}}},{'$group':{'_id':None,'count':{'$sum':1}}}],'cursor':{}}
    result=db.command({'explain':command,'verbosity':'executionStats'})
    from fastapi.encoders import jsonable_encoder
    return {'endpoint':endpoint,'collection':collection,'explain':jsonable_encoder(result,custom_encoder={ObjectId:str})}
