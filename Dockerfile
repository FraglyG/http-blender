FROM blenderkit/headless-blender:blender-5.2

USER root
RUN apt-get update && apt-get install -y python3-pip
RUN pip3 install flask

COPY server.py /server.py

EXPOSE 5000
CMD ["python3", "/server.py"]