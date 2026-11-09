import os
from flask import Response, request, abort, stream_with_context
from botocore.config import Config
import boto3


def filebase_enabled():
    required = (
        "FILEBASE_ACCESS_KEY_ID",
        "FILEBASE_SECRET_ACCESS_KEY",
        "FILEBASE_BUCKET",
        "FILEBASE_ENDPOINT",
    )
    return all(os.environ.get(name) for name in required)


def filebase_client():
    if not filebase_enabled():
        raise RuntimeError("Filebase storage is not configured.")

    return boto3.client(
        "s3",
        endpoint_url=os.environ["FILEBASE_ENDPOINT"],
        aws_access_key_id=os.environ["FILEBASE_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["FILEBASE_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("FILEBASE_REGION", "auto"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def upload_audio(file_object, filename):
    client = filebase_client()
    client.upload_fileobj(
        file_object,
        os.environ["FILEBASE_BUCKET"],
        filename,
        ExtraArgs={"ContentType": "audio/mpeg"},
    )


def delete_audio(filename):
    if not filebase_enabled():
        return

    filebase_client().delete_object(
        Bucket=os.environ["FILEBASE_BUCKET"],
        Key=filename,
    )


def stream_audio(filename, as_attachment=False):
    client = filebase_client()
    bucket = os.environ["FILEBASE_BUCKET"]

    try:
        info = client.head_object(Bucket=bucket, Key=filename)
    except Exception:
        abort(404)

    total_size = info["ContentLength"]
    range_header = request.headers.get("Range")
    byte_range = None
    status = 200
    start = 0
    end = total_size - 1

    if range_header:
        try:
            unit, value = range_header.split("=", 1)
            if unit.strip().lower() != "bytes" or "," in value:
                raise ValueError()

            first, last = value.strip().split("-", 1)

            if first:
                start = int(first)
                end = int(last) if last else total_size - 1
            else:
                suffix_length = int(last)
                if suffix_length <= 0:
                    raise ValueError()
                start = max(0, total_size - suffix_length)
                end = total_size - 1

            if start < 0 or start >= total_size or end < start:
                raise ValueError()

            end = min(end, total_size - 1)
            byte_range = f"bytes={start}-{end}"
            status = 206

        except (ValueError, TypeError):
            response = Response(status=416)
            response.headers["Content-Range"] = f"bytes */{total_size}"
            response.headers["Accept-Ranges"] = "bytes"
            return response

    try:
        options = {"Bucket": bucket, "Key": filename}
        if byte_range:
            options["Range"] = byte_range

        result = client.get_object(**options)
    except Exception:
        abort(503)

    body = result["Body"]

    def generate():
        try:
            for chunk in body.iter_chunks(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            body.close()

    response = Response(
        stream_with_context(generate()),
        status=status,
        content_type="audio/mpeg",
        direct_passthrough=True,
    )

    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(end - start + 1)
    response.headers["Cache-Control"] = "public, max-age=3600"

    if status == 206:
        response.headers["Content-Range"] = (
            f"bytes {start}-{end}/{total_size}"
        )

    disposition = "attachment" if as_attachment else "inline"
    response.headers["Content-Disposition"] = (
        f'{disposition}; filename="{filename}"'
    )

    return response


def upload_cover(file_object, filename):
    import mimetypes

    if not filename or os.path.basename(filename) != filename:
        raise ValueError("Invalid cover filename.")

    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    filebase_client().upload_fileobj(
        file_object,
        os.environ["FILEBASE_BUCKET"],
        f"covers/{filename}",
        ExtraArgs={"ContentType": content_type},
    )


def delete_cover(filename):
    if not filebase_enabled():
        return

    if not filename or os.path.basename(filename) != filename:
        raise ValueError("Invalid cover filename.")

    filebase_client().delete_object(
        Bucket=os.environ["FILEBASE_BUCKET"],
        Key=f"covers/{filename}",
    )


def stream_cover(filename):
    import mimetypes

    if not filename or os.path.basename(filename) != filename:
        abort(404)

    client = filebase_client()
    bucket = os.environ["FILEBASE_BUCKET"]
    key = f"covers/{filename}"

    try:
        info = client.head_object(Bucket=bucket, Key=key)
        result = client.get_object(Bucket=bucket, Key=key)
    except Exception:
        abort(404)

    body = result["Body"]

    def generate():
        try:
            for chunk in body.iter_chunks(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            body.close()

    response = Response(
        stream_with_context(generate()),
        content_type=info.get("ContentType")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream",
        direct_passthrough=True,
    )
    response.headers["Content-Length"] = str(info["ContentLength"])
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _validate_image_key(filename):
    if not filename or "\\" in filename:
        raise ValueError("Invalid image filename.")

    folder, separator, basename = filename.partition("/")

    if (
        folder not in {"djs", "ads", "listeners"}
        or not separator
        or not basename
        or "/" in basename
        or basename in {".", ".."}
    ):
        raise ValueError("Invalid image storage path.")

    return filename


def upload_image(file_object, filename):
    import mimetypes

    filename = _validate_image_key(filename)
    content_type = (
        mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    filebase_client().upload_fileobj(
        file_object,
        os.environ["FILEBASE_BUCKET"],
        filename,
        ExtraArgs={
            "ContentType": content_type,
            "CacheControl": "public, max-age=3600"
        }
    )


def delete_image(filename):
    if not filebase_enabled():
        return

    filename = _validate_image_key(filename)

    filebase_client().delete_object(
        Bucket=os.environ["FILEBASE_BUCKET"],
        Key=filename
    )


def stream_image(filename):
    filename = _validate_image_key(filename)

    client = filebase_client()
    bucket = os.environ["FILEBASE_BUCKET"]

    from botocore.exceptions import ClientError

    try:
        info = client.head_object(
            Bucket=bucket,
            Key=filename
        )
        result = client.get_object(
            Bucket=bucket,
            Key=filename
        )
    except ClientError as error:
        error_info = error.response.get("Error", {})
        status_code = error.response.get(
            "ResponseMetadata", {}
        ).get("HTTPStatusCode")
        error_code = error_info.get("Code")

        if status_code == 404 or error_code in {
            "404", "NoSuchKey", "NotFound"
        }:
            abort(404)

        abort(503)
    except Exception:
        abort(503)

    body = result["Body"]

    def generate():
        try:
            for chunk in body.iter_chunks(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            body.close()

    response = Response(
        stream_with_context(generate()),
        content_type=info.get("ContentType")
        or "application/octet-stream",
        direct_passthrough=True
    )

    response.headers["Content-Length"] = str(info["ContentLength"])
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["Content-Disposition"] = (
        f'inline; filename="{filename.split("/")[-1]}"'
    )
    response.headers["X-Content-Type-Options"] = "nosniff"

    return response
