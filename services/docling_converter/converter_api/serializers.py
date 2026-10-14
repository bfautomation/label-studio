from rest_framework import serializers


class DocumentConversionResponseSerializer(serializers.Serializer):
    document_id = serializers.CharField()
    filename = serializers.CharField()
    mime_type = serializers.CharField()
    markdown = serializers.CharField()
    converter = serializers.CharField()
    metadata = serializers.DictField()
