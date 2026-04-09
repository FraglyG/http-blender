from flask import Flask, request, send_file, jsonify
import subprocess
import os
import uuid

app = Flask(__name__)

# Configuration
UPLOAD_FOLDER = '/tmp/blender_tasks'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# This footer ensures the model is actually saved to a location we can access
# It selects all MESH objects created by the LLM's code and exports them.
EXPORT_FOOTER = """
import bpy
import os

# Deselect all, then select only meshes
bpy.ops.object.select_all(action='DESELECT')
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        obj.select_set(True)

# Export the selection to the specific path defined by the server
target_path = "{output_path}"
bpy.ops.export_scene.obj(filepath=target_path, use_selection=True)
"""

@app.route('/generate', methods=['POST'])
def generate_model():
    data = request.json
    if not data or 'code' not in data:
        return jsonify({"error": "No 'code' field provided"}), 400
    
    llm_code = data['code']
    task_id = str(uuid.uuid4())
    
    script_path = os.path.join(UPLOAD_FOLDER, f"{task_id}_script.py")
    output_path = os.path.join(UPLOAD_FOLDER, f"{task_id}.obj")
    
    # Inject Export Logic
    full_script = llm_code + "\n" + EXPORT_FOOTER.format(output_path=output_path)
    
    with open(script_path, "w") as f:
        f.write(full_script)
        
    try:
        cmd = [
            "blender",
            "--background",
            "--python", script_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        if os.path.exists(output_path):
            return send_file(output_path, as_attachment=True, download_name='model.obj')
        else:
            return jsonify({"error": "Blender ran but produced no output file."}), 500

    except subprocess.CalledProcessError as e:
        return jsonify({"error": "Blender crashed", "details": e.stderr.decode()}), 500
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)