from io import BytesIO
from typing import Any, Optional

import aleo_explorer_rust
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse

import disasm.aleo
from aleo_types import DeployTransaction, Deployment, Program, \
    AcceptedDeploy
from db import Database
from .template import htmx_template
from .utils import function_signature, out_of_sync_check


@htmx_template("programs.jinja2")
async def programs_route(request: Request):
    db: Database = request.app.state.db
    try:
        page = request.query_params.get("p")
        if page is None:
            page = 1
        else:
            page = int(page)
    except:
        raise HTTPException(status_code=400, detail="Invalid page")
    no_helloworld = request.query_params.get("no_helloworld", False)
    try:
        no_helloworld = bool(int(no_helloworld))
    except:
        no_helloworld = False
    total_programs = await db.get_program_count(no_helloworld=no_helloworld)
    total_pages = (total_programs // 50) + 1
    if page < 1 or page > total_pages:
        raise HTTPException(status_code=400, detail="Invalid page")
    start = 50 * (page - 1)
    programs = await db.get_programs(start, start + 50, no_helloworld=no_helloworld)
    builtin_programs = await db.get_builtin_programs()

    sync_info = await out_of_sync_check(request.app.state.session, db)
    ctx = {
        "programs": programs + builtin_programs,
        "page": page,
        "total_pages": total_pages,
        "no_helloworld": no_helloworld,
        "sync_info": sync_info,
    }
    return ctx, {'Cache-Control': 'public, max-age=15'}


@htmx_template("program.jinja2")
async def program_route(request: Request):
    db: Database = request.app.state.db
    program_id = request.query_params.get("id")
    if program_id is None:
        raise HTTPException(status_code=400, detail="Missing program id")
    block = await db.get_block_by_program_id(program_id)
    if block:
        transaction: DeployTransaction | None = None
        for ct in block.transactions:
            if isinstance(ct, AcceptedDeploy):
                tx = ct.transaction
                if isinstance(tx, DeployTransaction) and str(tx.deployment.program.id) == program_id:
                    transaction = tx
                    break
        if transaction is None:
            raise HTTPException(status_code=550, detail="Deploy transaction not found")
        deployment: Deployment = transaction.deployment
        program: Program = deployment.program
    else:
        program_bytes = await db.get_program(program_id)
        if not program_bytes:
            raise HTTPException(status_code=404, detail="Program not found")
        program = Program.load(BytesIO(program_bytes))
        transaction = None
    functions: list[str] = []
    for f in program.functions.keys():
        functions.append((await function_signature(db, str(program.id), str(f))).split("/", 1)[-1])
    leo_source = await db.get_program_leo_source_code(program_id)
    if leo_source is not None:
        source = leo_source
        has_leo_source = True
    else:
        source = disasm.aleo.disassemble_program(program)
        has_leo_source = False
    mappings: list[dict[str, str]] = []
    for name, mapping in program.mappings.items():
        mappings.append({
            "name": str(name),
            "key_type": str(mapping.key.plaintext_type),
            "value_type": str(mapping.value.plaintext_type)
        })
    address = await db.get_program_address(program_id)
    sync_info = await out_of_sync_check(request.app.state.session, db)
    ctx: dict[str, Any] = {
        "program_id": str(program.id),
        "times_called": await db.get_program_called_times(program_id),
        "imports": list(map(lambda i: str(i.program_id), program.imports)),
        "mappings": mappings,
        "structs": list(map(str, program.structs.keys())),
        "records": list(map(str, program.records.keys())),
        "closures": list(map(str, program.closures.keys())),
        "functions": functions,
        "source": source,
        "has_leo_source": has_leo_source,
        "recent_calls": await db.get_program_calls(program_id, 0, 30),
        "similar_count": await db.get_program_similar_count(program_id),
        "address": address,
        "sync_info": sync_info,
    }
    if transaction:
        ctx.update({
            "transaction_id": str(transaction.id),
            "owner": str(transaction.owner.address),
            "signature": str(transaction.owner.signature),
        })
    else:
        ctx.update({
            "transaction_id": None,
            "owner": None,
            "signature": None,
        })
    return ctx, {'Cache-Control': 'public, max-age=15'}


@htmx_template("similar_programs.jinja2")
async def similar_programs_route(request: Request):
    db: Database = request.app.state.db
    try:
        page = request.query_params.get("p")
        if page is None:
            page = 1
        else:
            page = int(page)
    except:
        raise HTTPException(status_code=400, detail="Invalid page")
    program_id = request.query_params.get("id")
    if program_id is None:
        raise HTTPException(status_code=400, detail="Missing program id")
    feature_hash = await db.get_program_feature_hash(program_id)
    if feature_hash is None:
        raise HTTPException(status_code=404, detail="Program not found")
    total_programs = await db.get_program_similar_count(program_id)
    total_pages = (total_programs // 50) + 1
    if page < 1 or page > total_pages:
        raise HTTPException(status_code=400, detail="Invalid page")
    start = 50 * (page - 1)
    programs = await db.get_programs_with_feature_hash(feature_hash, start, start + 50)

    sync_info = await out_of_sync_check(request.app.state.session, db)
    ctx = {
        "program_id": program_id,
        "programs": programs,
        "page": page,
        "total_pages": total_pages,
        "sync_info": sync_info,
    }
    return ctx, {'Cache-Control': 'public, max-age=15'}


@htmx_template("upload_source.jinja2")
async def upload_source_route(request: Request):
    db: Database = request.app.state.db
    program_id = request.query_params.get("id")
    if program_id is None:
        raise HTTPException(status_code=400, detail="Missing program id")
    program = await db.get_program(program_id)
    if program is None:
        raise HTTPException(status_code=404, detail="Program not found")
    if request.method == "POST":
        form = await request.form()
        source = form.get("source")
    else:
        source = ""
    imports: list[str] = []
    import_programs: list[Optional[str]] = []
    if (await db.get_program_leo_source_code(program_id)) is not None:
        has_leo_source = True
    else:
        has_leo_source = False
        program = Program.load(BytesIO(program))
        for i in program.imports:
            imports.append(str(i.program_id.name))
            if i.program_id != "credits.aleo":
                src = await db.get_program_leo_source_code(str(i.program_id))
                import_programs.append(src)
            else:
                import_programs.append(None)
    message = request.query_params.get("message")
    sync_info = await out_of_sync_check(request.app.state.session, db)
    ctx = {
        "program_id": program_id,
        "imports": imports,
        "import_programs": import_programs,
        "has_leo_source": has_leo_source,
        "message": message,
        "source": source,
        "sync_info": sync_info,
    }
    return ctx, {'Cache-Control': 'public, max-age=15'}


async def submit_source_route(request: Request):
    db: Database = request.app.state.db
    form = await request.form()
    program_id = form.get("id")
    if program_id is None or isinstance(program_id, UploadFile):
        return RedirectResponse(url=f"/upload_source?id={program_id}&message=Missing program id")
    program = await db.get_program(program_id)
    if program is None:
        return RedirectResponse(url=f"/upload_source?id={program_id}&message=Program not found")
    source = form.get("source")
    if source is None or isinstance(source, UploadFile) or source == "":
        return RedirectResponse(url=f"/upload_source?id={program_id}&message=Missing source code")
    imports = form.getlist("imports[]")
    import_programs = form.getlist("import_programs[]")
    if len(imports) != len(import_programs):
        return RedirectResponse(url=f"/upload_source?id={program_id}&message=Invalid form data")
    import_data: list[tuple[str, str]] = []
    for i, p in zip(imports, import_programs):
        if isinstance(i, UploadFile) or isinstance(p, UploadFile):
            return RedirectResponse(url=f"/upload_source?id={program_id}&message=Invalid form data")
        import_data.append((i, p))
    try:
        compiled = aleo_explorer_rust.compile_program(source, program_id.split(".")[0], import_data)
    except RuntimeError as e:
        if len(str(e)) > 255:
            msg = str(e)[:255] + "[trimmed]"
        else:
            msg = str(e)
        return RedirectResponse(url=f"/upload_source?id={program_id}&message=Failed to compile source code: {msg}")
    if program != compiled:
        return RedirectResponse(url=f"/upload_source?id={program_id}&message=Program compiled from source code doesn't match program on chain")
    await db.store_program_leo_source_code(program_id, source)
    return RedirectResponse(url=f"/program?id={program_id}", status_code=303)
